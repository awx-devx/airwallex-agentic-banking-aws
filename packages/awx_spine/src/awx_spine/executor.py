"""MutationExecutor: run one Operation at most once, whatever the network does.

1. store.begin(op) writes the record before anything is sent. A request_id already bound to a
   different body raises RequestIdReuse and nothing is sent.
2. A record that already holds a definitive answer (SUCCEEDED, or a FAILED rejection that is not
   retryable) is returned as is; the provider is not called again.
3. Runner rule 9. After an unknown outcome (timeout after submit, 5xx on a write) and on resume
   of a record that is still PENDING with attempts > 0 (the process died after the send), the
   executor reconciles BEFORE anything else: the step's reconcile hook when the runner passes
   one, else the generic per-endpoint read-back. Then:
   - found: the object must echo the operation's amount, currency and counterparty; a
     mismatch is OUTCOME_UNKNOWN `request_id_reused`, never SUCCEEDED and never FAILED;
   - shown not to have landed AND the endpoint deduplicates on request_id (endpoint table,
     unknown endpoints deny): resend the identical Operation;
   - anything else (no way to read it back, a read error, ReconcileUndetermined, or an endpoint
     that ignores request_id): stop at OUTCOME_UNKNOWN. Nothing is resent.
   It never mints a new request_id. files.upload is the one exception to "dedupes": a second
   upload is an inert file, so it may be resent without a read-back.
   Uncertain sends: the operation is UNCERTAIN from the first unknown outcome on, and that is
   stored (OperationRecord.may_have_landed, set by complete(), never cleared). A crashed
   PENDING record is first persisted as OUTCOME_UNKNOWN. While uncertain, every run reconciles
   before any send, whatever the last stored result (a 429 included), and no FAILED ever ends
   it: a refused resend, a 429 or a request that never left are all stored and returned as
   OUTCOME_UNKNOWN with the last error in body["last_error"] (a definitive refusal also in
   body["refused"]) and no top-level error_code or http_status. Nothing else is sent after a
   refusal in that call. A request_id_reused mismatch is OUTCOME_UNKNOWN too. Only a found,
   matching object, a mismatch, or a person resolves it. A read-back that finds two objects
   under one request_id (possible past the ~7-day deduplication window) is OUTCOME_UNKNOWN
   naming both.
4. Retries reuse the stored request_id and the identical body. Only retryable results are
   retried: 429 and a request that never left.
5. A duplicate-request rejection means the provider already holds this request_id. It resolves
   to the original object through reconcile or read-back (retried a bounded number of times in
   case the object is not listable yet), checked as in 3. Unresolved, it is OUTCOME_UNKNOWN.
6. files.upload operations carry an EvidenceRef, not bytes. The executor loads the bytes from the
   EvidenceStore, verifies size and SHA-256, and calls the port's upload(). Missing store, missing
   blob, a mismatch or an unusable file name is FAILED ValidationFailed and never retried.

7. Deduplication margin: a rule-9 resend relies on Airwallex deduplicating the
   request_id, which it does for about 7 days. Once the operation's first attempt
   (OperationRecord.first_attempt_at, written once) is older than DEDUP_RESEND_MARGIN (6 days,
   the kits' M1 margin), an empty read-back is never followed by a resend: the result is
   OUTCOME_UNKNOWN for a person. A record that has attempts but no first_attempt_at (written
   by a store that did not record first-attempt times) has an unknown age and is treated the
   same way (first_attempt_unknown).
8. Attempt lease: a PENDING record whose current attempt started
   (OperationRecord.attempt_started_at) less than attempt_lease_s ago belongs to a send that may
   still be in progress in another caller. The executor returns OUTCOME_UNKNOWN "in progress
   elsewhere" without a read-back, a send or a store write. Only an older PENDING attempt, or
   one with no start time, counts as crashed. A send that raises in this
   process is recorded OUTCOME_UNKNOWN before the exception propagates, so this process's own
   crash never looks like a live attempt.

The body goes to the port exactly as the kit built it; nothing is added.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from awx_common import AirwallexPort, Operation, OperationResult, OperationStatus, ReadRequest
from awx_common.errors import AwxError, OutcomeUnknown, StateConflict, ValidationFailed
from awx_common.evidence import (
    FILES_UPLOAD_OP,
    EvidenceIntegrityError,
    EvidenceRef,
    EvidenceStore,
    FileUploadPort,
    load_verified,
)
from awx_common.ops import DEDUP_RESEND_MARGIN as _DEDUP_RESEND_MARGIN
from awx_common.workflow import ReconcileUndetermined

from .endpoints import EndpointSpec, endpoint_for, extract_ids, mismatched_fields
from .normalise import result_from_error
from .store import PENDING, OperationStore

log = logging.getLogger("awx_spine.executor")

RECOVERED_FROM = "_awx_recovered_from"  # body marker: how the result was established
MISMATCH_CODE = "request_id_reused"

Reconcile = Callable[[Operation], Mapping[str, Any] | None]

# Airwallex deduplicates a request_id for about 7 days (builder guide). A rule-9 resend is allowed
# only while the first attempt is younger than this margin. Defined in awx_common.ops, which
# kits import it from, and re-exported here.
DEDUP_RESEND_MARGIN = _DEDUP_RESEND_MARGIN
# The attempt lease is longer than any live send. The deployed adapter Lambda may run one
# execute for up to ADAPTER_TIMEOUT_S (infra spine_stack: 4 x the 30 s client timeout + 15 s =
# 135 s; one execute can make several POSTs with logins and sleeps), so the lease is that plus a
# margin. A test in
# tests/contract pins DEFAULT_ATTEMPT_LEASE_S > ADAPTER_TIMEOUT_S.
DEFAULT_ATTEMPT_LEASE_S = 180.0
IN_PROGRESS_CODE = "in_progress_elsewhere"
PAST_MARGIN_CODE = "past_dedupe_margin"
FIRST_ATTEMPT_UNKNOWN_CODE = "first_attempt_unknown"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Found:
    obj: Mapping[str, Any] | None  # None: shown not to have landed
    determined: bool  # False: we cannot tell (no read-back, read error, undetermined)
    ambiguous: tuple[str, ...] = ()  # two or more objects under this request_id


_UNDETERMINED = _Found(None, False)


def _definitive(result: OperationResult) -> bool:
    if result.status is OperationStatus.SUCCEEDED:
        return True
    if result.status is OperationStatus.OUTCOME_UNKNOWN:
        return False
    err = result.error()
    return not (err is not None and err.retryable)


class MutationExecutor:
    def __init__(
        self,
        port: AirwallexPort,
        store: OperationStore,
        *,
        max_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        backoff_s: float = 1.0,
        duplicate_readback_retries: int = 3,
        evidence_store: EvidenceStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
        attempt_lease_s: float = DEFAULT_ATTEMPT_LEASE_S,
        dedupe_margin: timedelta = DEDUP_RESEND_MARGIN,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if attempt_lease_s < 0 or dedupe_margin <= timedelta(0):
            raise ValueError("attempt_lease_s must be >= 0 and dedupe_margin positive")
        self._clock = clock
        self._lease = timedelta(seconds=attempt_lease_s)
        self._dedupe_margin = dedupe_margin
        self._port = port
        self._store = store
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._backoff_s = backoff_s
        self._duplicate_readback_retries = duplicate_readback_retries
        self._evidence_store = evidence_store

    def run(self, op: Operation, *, reconcile: Reconcile | None = None) -> OperationResult:
        record = self._store.begin(op)  # raises RequestIdReuse before any network call
        crashed = record.status == PENDING and record.attempts > 0
        if not crashed and record.result is not None and _definitive(record.result):
            log.info("%s %s: already %s", op.name, op.request_id, record.status)
            return record.result
        spec = endpoint_for(op.name, op.path)
        if crashed and self._live(record.attempt_started_at):
            # Attempt lease: another caller's send may still be in flight. Touch nothing.
            log.info("%s %s: an attempt is in progress elsewhere", op.name, op.request_id)
            return OperationResult(
                request_id=op.request_id,
                status=OperationStatus.OUTCOME_UNKNOWN,
                error_type=OutcomeUnknown.__name__,
                error_code=IN_PROGRESS_CODE,
                error_message=f"{op.name}: an attempt is in progress elsewhere; not read or sent",
            )
        first_at = record.first_attempt_at
        attempted = record.attempts > 0
        result: OperationResult
        if crashed:
            # PENDING with attempts > 0: a send left and never completed, whatever an earlier
            # attempt's result was. Make that durable before reconciling.
            result = self._unknown(op, "a previous attempt was sent and never completed")
            record = self._store.complete(op.request_id, result)
            if record.result is not None and _definitive(record.result):
                return record.result  # another delivery settled it first: nothing to send
        else:
            result = record.result or self._unknown(op, "no attempt recorded")
        # Uncertain once any send may have landed without a known result. It is stored,
        # so it survives across calls (the adapter runs one send per call).
        uncertain = record.may_have_landed or (
            record.result is not None and record.result.status is OperationStatus.OUTCOME_UNKNOWN
        )
        unknown = uncertain
        for attempt in range(self._max_attempts):
            if unknown:
                # The provider may still be indexing the object (listing lagged create by up to
                # 1.9 s in the sandbox), so wait before the read-back.
                self._sleep(self._backoff_s * (2**attempt))
                settled = self._settle_unknown(op, spec, reconcile, result, first_at, attempted)
                if settled is not None:
                    self._store.complete(op.request_id, settled)
                    return settled
            elif attempt:
                self._sleep(self._backoff_s * (2 ** (attempt - 1)))
            first_at = self._store.record_attempt(op.request_id).first_attempt_at
            attempted = True
            try:
                result = self._send(op)
            except BaseException:
                # This process's own send died after it may have left: record that now, so it
                # is durable and is never mistaken for another caller's live attempt.
                self._store.complete(
                    op.request_id, self._unknown(op, "the send raised after it may have left")
                )
                raise
            if result.request_id != op.request_id:
                raise AssertionError("port returned a result for a different request_id")
            if result.error_type == "DuplicateRequest":
                result = self._resolve_duplicate(op, spec, reconcile, result)
            elif uncertain and self._refusal(spec, result):
                # An earlier send may still land, so this refusal settles nothing.
                result = self._still_uncertain(op, result)
                self._store.complete(op.request_id, result)
                return result
            if uncertain and self._held_as_failed(spec, result):
                # Never store a raw FAILED while uncertain; the loop still sees `result`.
                self._store.complete(op.request_id, self._still_uncertain(op, result))
            else:
                self._store.complete(op.request_id, result)
            if result.status is OperationStatus.OUTCOME_UNKNOWN:
                uncertain = True
                if result.error_type == "DuplicateRequest" or not self._may_resend(spec):
                    # Rule 9: resending a request_id the provider ignores (or already holds) is
                    # never safe, so the read-back decides on the next run, not a resend.
                    if result.error_type != "DuplicateRequest":
                        self._sleep(self._backoff_s)
                        settled = self._settle_unknown(
                            op, spec, reconcile, result, first_at, attempted
                        )
                        if settled is not None:
                            self._store.complete(op.request_id, settled)
                            return settled
                    return result
                unknown = True
                continue
            unknown = uncertain  # after a 429 on an uncertain op, reconcile again first
            if _definitive(result):
                return result
        if unknown:
            self._sleep(self._backoff_s * (2**self._max_attempts))
            settled = self._settle_unknown(op, spec, reconcile, result, first_at, attempted)
            if settled is not None:
                self._store.complete(op.request_id, settled)
                return settled
        if uncertain and self._held_as_failed(spec, result):
            # A retryable FAILED (429, not sent) after an uncertain send must not reach the
            # runner as FAILED: once its resends are spent it would become the step's result.
            result = self._still_uncertain(op, result)
            self._store.complete(op.request_id, result)
        log.warning(
            "%s %s: gave up after %d attempts (%s)",
            op.name,
            op.request_id,
            self._max_attempts,
            result.status,
        )
        return result

    # ---- rule 9 -------------------------------------------------------------------------------

    @staticmethod
    def _refusal(spec: EndpointSpec | None, result: OperationResult) -> bool:
        """A definitive FAILED from the provider that an uncertain send must not treat as final."""
        if spec is not None and spec.resend_safe:
            return False  # files.upload: a duplicate upload is inert
        return (
            result.status is OperationStatus.FAILED
            and _definitive(result)
            and result.error_type != "DuplicateRequest"
            and result.error_code != MISMATCH_CODE
        )

    @staticmethod
    def _held_as_failed(spec: EndpointSpec | None, result: OperationResult) -> bool:
        """Any FAILED an uncertain send keeps in flight: all but a mismatch, never files.upload."""
        if spec is not None and spec.resend_safe:
            return False  # files.upload: a duplicate upload is inert
        return result.status is OperationStatus.FAILED and result.error_code != MISMATCH_CODE

    @staticmethod
    def _still_uncertain(op: Operation, failed: OperationResult) -> OperationResult:
        """The FAILED this send got settles nothing, because an earlier send may land.

        The error goes in body["last_error"] (and, for a definitive refusal, also in
        body["refused"]); top-level error_code and http_status stay unset so nothing that
        matches on them can mistake this for the provider's answer to the original send.
        """
        detail = {
            "error_type": failed.error_type,
            "error_code": failed.error_code,
            "http_status": failed.http_status,
            "error_message": failed.error_message,
        }
        err = failed.error()
        retryable = err is not None and err.retryable
        log.warning(
            "%s %s: resend got %s %s after an unknown outcome; staying OUTCOME_UNKNOWN",
            op.name,
            op.request_id,
            failed.error_type,
            failed.error_code,
        )
        body = {k: v for k, v in (failed.body or {}).items() if k not in ("refused", "last_error")}
        body["last_error"] = detail
        if not retryable:
            body["refused"] = detail
        what = "was retryable" if retryable else "was refused"
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            provider_id=failed.provider_id,
            error_type=OutcomeUnknown.__name__,
            error_message=(
                f"{op.name}: resend {what} ({failed.error_type} {failed.error_code}: "
                f"{failed.error_message}); an earlier send may still land"
            ),
            body=body,
        )

    @staticmethod
    def _ambiguous(op: Operation, ids: tuple[str, ...]) -> OperationResult:
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_type=OutcomeUnknown.__name__,
            error_code="multiple_objects",
            error_message=(
                f"{op.name}: {len(ids)} objects hold request_id {op.request_id} "
                f"({', '.join(ids)}); the money may have moved more than once, a person decides"
            ),
            body={"provider_ids": list(ids)},
        )

    @staticmethod
    def _may_resend(spec: EndpointSpec | None) -> bool:
        return spec is not None and spec.may_resend_after_unknown

    def _settle_unknown(
        self,
        op: Operation,
        spec: EndpointSpec | None,
        reconcile: Reconcile | None,
        last: OperationResult,
        first_at: datetime | None = None,
        attempted: bool = False,
    ) -> OperationResult | None:
        """A final result, or None to resend the identical Operation."""
        found = self._find(op, spec, reconcile)
        if found.ambiguous:
            return self._ambiguous(op, found.ambiguous)
        if found.obj is not None:
            return self._accept(op, spec, found.obj, "reconcile" if reconcile else "read_back")
        if spec is not None and spec.resend_safe:
            return None  # files.upload: a duplicate is inert
        if found.determined and spec is not None and spec.dedupes:
            if first_at is None and attempted:
                # A record with attempts but no first_attempt_at has an unknown age, so it may
                # be past the provider's window: never resend.
                return OperationResult(
                    request_id=op.request_id,
                    status=OperationStatus.OUTCOME_UNKNOWN,
                    error_type=OutcomeUnknown.__name__,
                    error_code=FIRST_ATTEMPT_UNKNOWN_CODE,
                    error_message=(
                        f"{op.name}: the first attempt's time is unknown (the record has none), "
                        "so it may be past the deduplication margin; not resent, a person "
                        "decides"
                    ),
                    body=self._carry_last_error(last),
                )
            if first_at is not None and self._clock() - first_at > self._dedupe_margin:
                # Past the margin the provider may no longer deduplicate this request_id.
                return OperationResult(
                    request_id=op.request_id,
                    status=OperationStatus.OUTCOME_UNKNOWN,
                    error_type=OutcomeUnknown.__name__,
                    error_code=PAST_MARGIN_CODE,
                    error_message=(
                        f"{op.name}: first attempt {first_at.isoformat()} is older than the "
                        f"{self._dedupe_margin.days}-day deduplication margin; not resent, "
                        "a person decides"
                    ),
                    body=self._carry_last_error(last),
                )
            return None
        if last.status is OperationStatus.OUTCOME_UNKNOWN:
            return last
        if last.status is OperationStatus.FAILED and self._held_as_failed(spec, last):
            return self._still_uncertain(op, last)  # keeps last_error
        return self._unknown(op, "could not establish whether the write landed")

    def _live(self, started: datetime | None) -> bool:
        return started is not None and self._clock() - started < self._lease

    def _carry_last_error(self, last: OperationResult) -> dict[str, Any] | None:
        if last.status is OperationStatus.FAILED:
            return {"last_error": self._detail(last)}
        if last.body and "last_error" in last.body:
            return {"last_error": last.body["last_error"]}
        return None

    @staticmethod
    def _detail(failed: OperationResult) -> dict[str, Any]:
        return {
            "error_type": failed.error_type,
            "error_code": failed.error_code,
            "http_status": failed.http_status,
            "error_message": failed.error_message,
        }

    def _find(
        self, op: Operation, spec: EndpointSpec | None, reconcile: Reconcile | None
    ) -> _Found:
        try:
            if reconcile is not None:
                return _Found(reconcile(op), True)
            if spec is None or spec.readback is None:
                return _UNDETERMINED
            return self._generic_read_back(op, spec)
        except ReconcileUndetermined as err:
            log.info("%s %s: reconcile undetermined (%s)", op.name, op.request_id, err)
            return _UNDETERMINED
        except AwxError as err:
            log.info(
                "%s %s: read-back failed (%s); outcome still unknown",
                op.name,
                op.request_id,
                type(err).__name__,
            )
            return _UNDETERMINED

    def _generic_read_back(self, op: Operation, spec: EndpointSpec) -> _Found:
        rb = spec.readback
        assert rb is not None
        if rb.body_field is None:
            value: Any = op.request_id
            match_field = "request_id"
        else:
            value = op.body.get(rb.body_field)
            match_field = rb.body_field
            if not isinstance(value, str) or not value:
                return _UNDETERMINED
        body = self._port.read(
            ReadRequest(rb.list_path, {rb.query_field: value}, on_behalf_of=op.on_behalf_of)
        )
        items = body.get("items")
        if not isinstance(items, list):
            return _UNDETERMINED
        matches = [i for i in items if isinstance(i, Mapping) and i.get(match_field) == value]
        distinct: dict[str, Mapping[str, Any]] = {}
        for m in matches:  # the same object listed twice (a page overlap) is one object
            pid = extract_ids(spec, m)[0]
            distinct.setdefault(pid if pid is not None else f"<no id> {dict(m)!r}", m)
        if len(distinct) > 1:
            return _Found(None, True, tuple(distinct))
        return _Found(next(iter(distinct.values())) if distinct else None, True)

    def _accept(
        self, op: Operation, spec: EndpointSpec | None, obj: Mapping[str, Any], how: str
    ) -> OperationResult:
        """The found object as this operation's result, or OUTCOME_UNKNOWN request_id_reused
        when it does not match this operation."""
        wrong = mismatched_fields(spec, op.body, obj)
        provider_id, provider_status = extract_ids(spec, obj)
        if wrong:
            err = StateConflict(
                f"{op.name}: the object holding request_id {op.request_id} does not match this "
                f"operation ({', '.join(wrong)}); refusing it",
                code=MISMATCH_CODE,
                details={"fields": wrong, "provider_id": provider_id},
            )
            log.warning("%s %s: read-back mismatch on %s", op.name, op.request_id, wrong)
            # Never a definitive FAILED. The object under our request_id
            # may be this kit's own earlier body, written through another store (local_runner
            # and the deployed DynamoDB store share one sandbox account), so money may have
            # moved. OUTCOME_UNKNOWN for a person, the code kept at the top level.
            # The one mismatch shape: another object's id and status never read
            # as ours at the top level; the id found is in body["details"]["provider_id"].
            failed = result_from_error(op, err)
            return OperationResult(
                request_id=op.request_id,
                status=OperationStatus.OUTCOME_UNKNOWN,
                error_type=OutcomeUnknown.__name__,
                error_code=MISMATCH_CODE,
                error_message=err.message + "; money may have moved, a person decides",
                body={**(failed.body or {}), "last_error": self._detail(failed)},
            )
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.SUCCEEDED,
            http_status=200,
            provider_id=provider_id,
            provider_status=provider_status,
            body={**obj, RECOVERED_FROM: how},
        )

    def _resolve_duplicate(
        self,
        op: Operation,
        spec: EndpointSpec | None,
        reconcile: Reconcile | None,
        rejected: OperationResult,
    ) -> OperationResult:
        found = self._find(op, spec, reconcile)
        for i in range(self._duplicate_readback_retries):
            if found.obj is not None or found.ambiguous or not found.determined:
                break
            self._sleep(self._backoff_s * (i + 1))
            found = self._find(op, spec, reconcile)
        if found.ambiguous:
            return self._ambiguous(op, found.ambiguous)
        if found.obj is not None:
            return self._accept(op, spec, found.obj, "duplicate_request")
        # The provider holds this request_id but we cannot show the object matches this
        # operation, so we cannot call it ours. Reconciliation decides.
        # Like every converted result it carries body["last_error"]; the duplicate
        # details also stay at the top level and in the rest of the body.
        return replace(
            rejected,
            status=OperationStatus.OUTCOME_UNKNOWN,
            error_message=(rejected.error_message or "duplicate request")
            + " (the provider already holds this request_id; it could not be read back)",
            body={**(rejected.body or {}), "last_error": self._detail(rejected)},
        )

    @staticmethod
    def _unknown(op: Operation, why: str) -> OperationResult:
        return result_from_error(op, OutcomeUnknown(f"{op.name}: {why}"))

    # ---- sending ------------------------------------------------------------------------------

    def _send(self, op: Operation) -> OperationResult:
        if op.name != FILES_UPLOAD_OP:
            return self._port.execute(op)
        return self._upload(op)

    def _upload(self, op: Operation) -> OperationResult:
        def refuse(message: str) -> OperationResult:
            return result_from_error(op, ValidationFailed(message, code="evidence_invalid"))

        if self._evidence_store is None:
            return refuse("files.upload needs an evidence store; none configured")
        if not isinstance(self._port, FileUploadPort):
            return refuse("the port cannot upload files")
        try:
            ref = EvidenceRef.from_body(op.body)
            data = load_verified(self._evidence_store, ref)
        except (EvidenceIntegrityError, KeyError, TypeError) as err:
            return refuse(f"evidence rejected: {err}")
        notes = op.body.get("notes")
        try:
            body = self._port.upload(
                data,
                file_name=ref.file_name,
                content_type=ref.content_type,
                notes=notes if isinstance(notes, str) else None,
                on_behalf_of=op.on_behalf_of,
            )
        except AwxError as err:
            return result_from_error(op, err)
        except ValueError as err:  # e.g. a file name the multipart header cannot carry
            return refuse(f"evidence rejected: {err}")
        file_id = body.get("file_id") if isinstance(body, Mapping) else None
        return OperationResult(
            request_id=op.request_id,
            status=OperationStatus.SUCCEEDED,
            http_status=201,
            provider_id=str(file_id) if file_id else None,
            body=dict(body),
        )
