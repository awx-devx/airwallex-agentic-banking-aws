from awx_common.ops import ReadRequest


def read_balances(args, ctx):
    return ctx.read(ReadRequest(path="/api/v1/balances/current"))


READS = {"read_balances": read_balances}
