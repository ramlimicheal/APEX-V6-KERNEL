"""Postconditions for the 40 QuixBugs Python programs.

Each SPECS entry is one line:
  args   argument types ("arr: list[int], k: int")
  pre    the precondition the QuixBugs docstring states (also used by the
         equivalence mode)
  post   the postcondition, over the arguments (their values on entry) and
         `result`
  cite   the docstring line the spec comes from, quoted verbatim
  domain extra int bounds that keep checking finite (NOT part of the
         documented spec; listed so every "proved up to bound" is explicit).
         Used by both modes.
  partial True when the post captures only part of the documented output
  cap    max length of list/str inputs in spec mode (default K = 8); lowered
         per program where symbolic lengths made the solver too slow

The helper predicates below are plain Python in the subset APEX models, so
the SAME code is evaluated symbolically by apex.pybounded and concretely
when a counterexample is replayed against the real QuixBugs file.

For programs the engine rejects (graphs, dicts, floats, generators, ...), the
post is written for the record only: the engine rejects the program before
it ever evaluates the post, and their helper names are not defined here.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Spec:
    args: str
    pre: str
    post: str
    cite: str
    domain: str = "True"
    partial: bool = False
    cap: int = 8


# ---------------------------------------------------------------------------
# Helper predicates (pure, loop-bounded, in the modeled subset)
# ---------------------------------------------------------------------------

def is_sorted(xs):
    for i in range(1, len(xs)):
        if xs[i - 1] > xs[i]:
            return False
    return True


def count(xs, v):
    c = 0
    for y in xs:
        if y == v:
            c += 1
    return c


def is_perm(xs, ys):
    if len(xs) != len(ys):
        return False
    for x in xs:
        if count(xs, x) != count(ys, x):
            return False
    return True


def all_distinct(xs):
    for i in range(len(xs)):
        for j in range(i):
            if xs[i] == xs[j]:
                return False
    return True


def count_lt(xs, v):
    return sum(1 for x in xs if x < v)


def count_le(xs, v):
    return sum(1 for x in xs if x <= v)


def popcount(n):
    c = 0
    while n > 0:
        c += n % 2
        n //= 2
    return c


def divides(d, x):
    return x == 0 if d == 0 else x % d == 0


def is_gcd(g, a, b):
    if not (divides(g, a) and divides(g, b)):
        return False
    for d in range(1, max(a, b) + 1):
        if d > g and a % d == 0 and b % d == 0:
            return False
    return True


def is_prime(p):
    if p < 2:
        return False
    for d in range(2, p):
        if p % d == 0:
            return False
    return True


def all_prime(xs):
    return all(is_prime(x) for x in xs)


def product(xs):
    r = 1
    for x in xs:
        r = r * x
    return r


def is_balanced(s):
    for i in range(len(s) + 1):
        if count(s[:i], '(') < count(s[:i], ')'):
            return False
    return count(s, '(') == count(s, ')')


def edit_distance(s, t):
    prev = [j for j in range(len(t) + 1)]
    for i in range(1, len(s) + 1):
        cur = [i]
        for j in range(1, len(t) + 1):
            cost = 0 if s[i - 1] == t[j - 1] else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[len(t)]


def lis_ref(arr):
    best = []
    for i in range(len(arr)):
        b = 1
        for j in range(i):
            if arr[j] < arr[i] and best[j] + 1 > b:
                b = best[j] + 1
        best.append(b)
    return max(best) if best else 0


def is_subseq(sub, s):
    i = 0
    for ch in s:
        if i < len(sub) and sub[i] == ch:
            i += 1
    return i == len(sub)


def lcs_len(a, b):
    prev = [0 for _ in range(len(b) + 1)]
    for i in range(1, len(a) + 1):
        cur = [0]
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur.append(prev[j - 1] + 1)
            else:
                cur.append(max(prev[j], cur[j - 1]))
        prev = cur
    return prev[len(b)]


def max_sublist_ref(arr):
    best = 0
    for i in range(len(arr)):
        s = 0
        for j in range(i, len(arr)):
            s += arr[j]
            best = max(best, s)
    return best


def is_digit_list(ds):
    if len(ds) == 0 or (len(ds) > 1 and ds[0] == 0):
        return False
    return all(0 <= d <= 9 for d in ds)


def is_palindrome(xs):
    for i in range(len(xs)):
        if xs[i] != xs[len(xs) - 1 - i]:
            return False
    return True


def digits_value(ds):
    v = 0
    for d in ds:
        v = v * 10 + d
    return v


def is_pal_int(m):
    ds = []
    while m > 0:
        ds.append(m % 10)
        m //= 10
    return is_palindrome(ds)


def next_pal_ref(n):
    m = n + 1
    while not is_pal_int(m):
        m += 1
    return m


def is_desc_sorted(xs):
    for i in range(1, len(xs)):
        if xs[i - 1] < xs[i]:
            return False
    return True


def lex_less(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return a[i] < b[i]
    return len(a) < len(b)


def binom(n, k):
    if k < 0 or k > n:
        return 0
    b = 1
    for i in range(k):
        b = b * (n - i) // (i + 1)
    return b


def pascal_row_ok(row, r):
    return len(row) == r + 1 and all(row[c] == binom(r, c) for c in range(r + 1))


def change_ref(coins, total):
    if total < 0:
        return 0
    ways = [1] + [0] * total
    for c in coins:
        for t in range(c, total + 1):
            ways[t] = ways[t] + ways[t - c]
    return ways[total]


def pow2(n):
    p = 1
    for _ in range(n):
        p = p * 2
    return p


def pairwise_distinct(xss):
    for i in range(len(xss)):
        for j in range(i):
            if xss[i] == xss[j]:
                return False
    return True


def all_subseqs(subs, arr):
    return all(is_subseq(s, arr) for s in subs)


def combos(n, k):
    if n < 0 or k < 0 or k > n:
        return 0
    row = [1]
    for _ in range(n):
        row = [1] + [row[j] + row[j + 1] for j in range(len(row) - 1)] + [1]
    return row[k]


def is_asc_in(s, a, b, k):
    if len(s) != k:
        return False
    for i in range(len(s)):
        if s[i] < a or s[i] >= b or (i > 0 and s[i - 1] >= s[i]):
            return False
    return True


def primes_upto(m):
    return [p for p in range(2, m + 1) if is_prime(p)]


def digit_value(ch):
    c = ord(ch)
    return c - 48 if c <= 57 else c - 55


def is_base_repr(s, b):
    if len(s) == 0 or s[0] == '0':
        return False
    for ch in s:
        c = ord(ch)
        if not ((48 <= c <= 57) or (65 <= c <= 90)) or digit_value(ch) >= b:
            return False
    return True


def from_base(s, b):
    v = 0
    for ch in s:
        v = v * b + digit_value(ch)
    return v


# ---------------------------------------------------------------------------
# One spec per QuixBugs program
# ---------------------------------------------------------------------------

SPECS = {
    "bitcount": Spec("n: int", "n >= 0", "result == popcount(n)", "Output: The number of 1-bits in the binary encoding of n", "n <= 255"),
    "breadth_first_search": Spec("startnode: node, goalnode: node", "True", "result == reachable(startnode, goalnode)", "Output: Whether goalnode is reachable from startnode"),
    "bucketsort": Spec("arr: list[int], k: int", "all(0 <= x < k for x in arr)", "is_sorted(result) and is_perm(result, arr)", "Output: The elements of arr in sorted order", "k <= 8", cap=4),
    "depth_first_search": Spec("startnode: node, goalnode: node", "True", "result == reachable(startnode, goalnode)", "Output: Whether goalnode is reachable from startnode"),
    "detect_cycle": Spec("node: node", "True", "result == is_cyclic(node)", "Output: Whether the linked list is cyclic"),
    "find_first_in_sorted": Spec("arr: list[int], x: int", "is_sorted(arr)", "(result == -1 and x not in arr) or (0 <= result < len(arr) and arr[result] == x and x not in arr[:result])", "Output: The lowest index i such that arr[i] == x, or -1 if x not in arr"),
    "find_in_sorted": Spec("arr: list[int], x: int", "is_sorted(arr)", "(result == -1 and x not in arr) or (0 <= result < len(arr) and arr[result] == x)", "Output: An index i such that arr[i] == x, or -1 if x not in arr"),
    "flatten": Spec("arr: list[any]", "True", "list(result) == flat_ref(arr)", "Output: A generator for the input's non-list objects"),
    "gcd": Spec("a: int, b: int", "a >= 0 and b >= 0", "is_gcd(result, a, b)", "Output: The greatest int that divides evenly into a and b", "a <= 20 and b <= 20"),
    "get_factors": Spec("n: int", "n >= 1", "is_sorted(result) and all_prime(result) and product(result) == n", "Output: A list of the prime factors of n in sorted order with repetition", "n <= 50"),
    "hanoi": Spec("height: int, start: int, end: int", "height >= 0 and 1 <= start <= 3 and 1 <= end <= 3", "is_shortest_hanoi(result, height, start, end)", "Output: An ordered list of pairs (a, b) representing the shortest series of steps (each step moving the top disk from peg a to peg b) that solves the puzzle."),
    "is_valid_parenthesization": Spec("parens: str", "all(ch == '(' or ch == ')' for ch in parens)", "result == is_balanced(parens)", "Output: Whether the parentheses are properly nested"),
    "kheapsort": Spec("arr: list[int], k: int", "all_distinct(arr)", "list(result) == sorted(arr)", "Output: A generator that yields the elements of arr in sorted order"),
    "knapsack": Spec("capacity: int, items: list[tuple]", "True", "result == knapsack_ref(capacity, items)", "Output: The maximum total value of any combination of items that the knapsack can hold"),
    "kth": Spec("arr: list[int], k: int", "0 <= k < len(arr)", "count_lt(arr, result) <= k < count_le(arr, result)", "Output: The kth-lowest element of arr (0-based)", cap=4),
    "lcs_length": Spec("s: str, t: str", "True", "result == longest_common_substring_len(s, t)", "Output: Length of the longest substring common to s and t"),
    "levenshtein": Spec("source: str, target: str", "True", "result == edit_distance(source, target)", "Output: The Levenshtein distance between the source and target.", cap=4),
    "lis": Spec("arr: list[int]", "all_distinct(arr)", "result == lis_ref(arr)", "Output: The length of the longest monotonically increasing subsequence of arr", cap=4),
    "longest_common_subsequence": Spec("a: str, b: str", "True", "is_subseq(result, a) and is_subseq(result, b) and len(result) == lcs_len(a, b)", "Output: The longest string which is a subsequence of both strings.", cap=4),
    "max_sublist_sum": Spec("arr: list[int]", "True", "result == max_sublist_ref(arr)", "Efficient equivalent to max(sum(arr[i:j]) for 0 <= i <= j <= len(arr))"),
    "mergesort": Spec("arr: list[int]", "True", "is_sorted(result) and is_perm(result, arr)", "Output: The elements of arr in sorted order", cap=4),
    "minimum_spanning_tree": Spec("weight_by_edge: dict", "is_connected(weight_by_edge)", "is_min_spanning_tree(result, weight_by_edge)", "Output: A set of edges that connects all the vertices of the input graph and has the least possible total weight."),
    "next_palindrome": Spec("digit_list: list[int]", "is_digit_list(digit_list) and is_palindrome(digit_list)", "is_digit_list(result) and digits_value(result) == next_pal_ref(digits_value(digit_list))", "Output: An array which represents the next palindrome", cap=3),
    "next_permutation": Spec("perm: list[int]", "all_distinct(perm) and not is_desc_sorted(perm)", "is_perm(result, perm) and lex_less(perm, result)", "Output: The lexicographically next permutation of the elements of perm", partial=True, cap=4),
    "pascal": Spec("n: int", "n >= 1", "len(result) == n and all(pascal_row_ok(result[r], r) for r in range(n))", "Output: The first n rows of Pascal's triangle as a list of n lists", "n <= 6"),
    "possible_change": Spec("coins: list[int], total: int", "all(c > 0 for c in coins)", "result == change_ref(coins, total)", "Output: The number of distinct ways to make change adding up to total using only coins of the given values.", "total <= 4", cap=4),
    "powerset": Spec("arr: list[int]", "all_distinct(arr)", "len(result) == pow2(len(arr)) and all_subseqs(result, arr) and pairwise_distinct(result)", "Output: A list of lists, each representing a different subset of arr.", cap=3),
    "quicksort": Spec("arr: list[int]", "True", "is_sorted(result) and is_perm(result, arr)", "Output: The elements of arr in sorted order", cap=4),
    "reverse_linked_list": Spec("node: node", "is_acyclic(node)", "is_reversed_list(result, node)", "Output: The head of the reversed linked list"),
    "rpn_eval": Spec("tokens: list[float|str]", "True", "result == rpn_ref(tokens)", "Example: rpn_eval([3.0, 5.0, '+', 2.0, '/']) == 4.0 (four-function RPN calculator)"),
    "shortest_path_length": Spec("length_by_edge: dict, startnode: node, goalnode: node", "all_positive(length_by_edge)", "result == dijkstra_ref(length_by_edge, startnode, goalnode)", "Output: The length of the shortest path from startnode to goalnode in the input graph"),
    "shortest_path_lengths": Spec("n: int, length_by_edge: dict", "no_negative_cycles(length_by_edge)", "result == floyd_warshall_ref(n, length_by_edge)", "Output: A dict containing shortest path length keyed by an ordered pair of node ids"),
    "shortest_paths": Spec("source: str, weight_by_edge: dict", "no_negative_cycles(weight_by_edge)", "result == bellman_ford_ref(source, weight_by_edge)", "Output: A dict mapping each node id to the minimum weight of a path from the source node to that node"),
    "shunting_yard": Spec("tokens: list[int|str]", "True", "result == infix_to_rpn_ref(tokens)", "Output: The input tokens reordered into Reverse Polish Notation"),
    "sieve": Spec("max: int", "max >= 1", "result == primes_upto(max)", "Output: A list containing all primes up to and including max", "max <= 9"),
    "sqrt": Spec("x: float, epsilon: float", "x >= 1 and epsilon > 0", "abs(result - x ** 0.5) <= epsilon", "Output: A float in the interval [sqrt(x) - epsilon, sqrt(x) + epsilon]"),
    "subsequences": Spec("a: int, b: int, k: int", "k >= 1", "len(result) == combos(b - a, k) and all(is_asc_in(s, a, b, k) for s in result) and pairwise_distinct(result)", "Output: A list of all length-k ascending sequences of ints in range(a, b)", "-1 <= b - a <= 4 and k <= 3"),
    "to_base": Spec("num: int, b: int", "num > 0 and 2 <= b <= 36", "is_base_repr(result, b) and from_base(result, b) == num", "Output: A string representing the value of num in base b.", "num <= 255"),
    "topological_ordering": Spec("nodes: list[node]", "is_acyclic_graph(nodes)", "is_topological_order(result, nodes)", "Output: An OrderedSet containing the elements of nodes in an order that puts each node before all the nodes it has edges to"),
    "wrap": Spec("text: str, cols: int", "cols > 0", "all(len(line) <= cols for line in result) and ''.join(result) == text", "Output: An ordered list of strings, each no longer than the column width, such that the concatenation of the strings returns the original text"),
}

# Programs the bounded-equivalence mode targets (step 4d); all others are
# expected to be rejected (graphs, dicts, floats, generators, ...).
EQUIV_SCOPE = [
    "bitcount", "bucketsort", "find_first_in_sorted", "find_in_sorted", "gcd", "get_factors",
    "is_valid_parenthesization", "kth", "levenshtein", "longest_common_subsequence",
    "max_sublist_sum", "mergesort", "next_palindrome", "next_permutation", "pascal",
    "possible_change", "powerset", "quicksort", "sieve", "subsequences", "to_base", "lis",
]
