# A max-of-two that has a real bug: it ignores the case where b > a but the
# author wrote the wrong comparison. Verify with:
#   python -m apex --verify-code examples/buggy_max.py \
#       --postcondition "result >= a and result >= b"
# pyverify will return CONCRETE inputs that break the contract.
def my_max(a, b):
    if a > b:
        return a
    return a
