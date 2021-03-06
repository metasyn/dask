import pytest

from dask.order import ndependencies, order
from dask.core import get_deps
from dask.utils_test import add, inc


@pytest.fixture(params=['abcde', 'edcba'])
def abcde(request):
    return request.param


def issorted(L, reverse=False):
    return sorted(L, reverse=reverse) == L


def f(*args):
    pass


def test_ordering_keeps_groups_together(abcde):
    a, b, c, d, e = abcde
    d = dict(((a, i), (f,)) for i in range(4))
    d.update({(b, 0): (f, (a, 0), (a, 1)),
              (b, 1): (f, (a, 2), (a, 3))})
    o = order(d)

    assert abs(o[(a, 0)] - o[(a, 1)]) == 1
    assert abs(o[(a, 2)] - o[(a, 3)]) == 1

    d = dict(((a, i), (f,)) for i in range(4))
    d.update({(b, 0): (f, (a, 0), (a, 2)),
              (b, 1): (f, (a, 1), (a, 3))})
    o = order(d)

    assert abs(o[(a, 0)] - o[(a, 2)]) == 1
    assert abs(o[(a, 1)] - o[(a, 3)]) == 1


def test_avoid_broker_nodes(abcde):
    """

    b0    b1  b2
    |      \  /
    a0      a1

    a0 should be run before a1
    """
    a, b, c, d, e = abcde
    dsk = {(a, 0): (f,), (a, 1): (f,),
           (b, 0): (f, (a, 0)), (b, 1): (f, (a, 1)), (b, 2): (f, (a, 1))}

    o = order(dsk)

    assert o[(a, 0)] < o[(a, 1)]

    # Switch name of 0, 1 to ensure that this isn't due to string comparison
    dsk = {(a, 1): (f,), (a, 0): (f,),
           (b, 0): (f, (a, 1)), (b, 1): (f, (a, 0)), (b, 2): (f, (a, 0))}

    o = order(dsk)

    assert o[(a, 0)] > o[(a, 1)]


def test_base_of_reduce_preferred(abcde):
    """
               a3
              /|
            a2 |
           /|  |
         a1 |  |
        /|  |  |
      a0 |  |  |
      |  |  |  |
      b0 b1 b2 b3
        \ \ / /
           c

    We really want to run b0 quickly
    """
    a, b, c, d, e = abcde
    dsk = {(a, i): (f, (a, i - 1), (b, i)) for i in [1, 2, 3]}
    dsk[(a, 0)] = (f, (b, 0))
    dsk.update({(b, i): (f, c, 1) for i in [0, 1, 2, 3]})
    dsk[c] = 1

    o = order(dsk)

    assert o == {(a, 3): 0,
                 (a, 2): 1,
                 (a, 1): 2,
                 (a, 0): 3,
                 (b, 0): 4,
                 c: 5,
                 (b, 1): 6,
                 (b, 2): 7,
                 (b, 3): 8}

    # (b, 0) is the most important out of (b, i)
    assert min([(b, i) for i in [0, 1, 2, 3]], key=o.get) == (b, 0)


def test_avoid_upwards_branching(abcde):
    """
         a1
         |
         a2
         |
         a3    d1
        /  \  /
      b1    c1
      |     |
      b2    c2
            |
            c3

    Prefer b1 over c1 because it won't stick around waiting for d1 to complete
    """
    a, b, c, d, e = abcde
    dsk = {(a, 1): (f, (a, 2)),
           (a, 2): (f, (a, 3)),
           (a, 3): (f, (b, 1), (c, 1)),
           (b, 1): (f, (b, 2)),
           (c, 1): (f, (c, 2)),
           (c, 2): (f, (c, 3)),
           (d, 1): (f, (c, 1))}

    o = order(dsk)

    assert o[(b, 1)] < o[(c, 1)]


def test_avoid_upwards_branching_complex(abcde):
    """
         a1
         |
    e2   a2  d2  d3
    |    |    \  /
    e1   a3    d1
     \  /  \  /
      b1    c1
      |     |
      b2    c2
            |
            c3

    Prefer c1 over b1 because c1 will stay in memory less long while b1
    computes
    """
    a, b, c, d, e = abcde
    dsk = {(a, 1): (f, (a, 2)),
           (a, 2): (f, (a, 3)),
           (a, 3): (f, (b, 1), (c, 1)),
           (b, 1): (f, (b, 2)),
           (c, 1): (f, (c, 2)),
           (c, 2): (f, (c, 3)),
           (d, 1): (f, (c, 1)),
           (d, 2): (f, (d, 1)),
           (d, 3): (f, (d, 1)),
           (e, 1): (f, (b, 1)),
           (e, 2): (f, (e, 1))}

    o = order(dsk)

    assert o[(c, 1)] < o[(b, 1)]


@pytest.mark.xfail(reason="this case is ambiguous")
def test_deep_bases_win_over_dependents(abcde):
    """
    It's not clear who should run first, e or d

    1.  d is nicer because it exposes parallelism
    2.  e is nicer (hypothetically) because it will be sooner released
        (though in this case we need d to run first regardless)

            a
          / | \   .
         b  c |
        / \ | /
       e    d
    """
    a, b, c, d, e = abcde
    dsk = {a: (f, b, c, d), b: (f, d, e), c: (f, d), d: 1, e: 2}

    o = order(dsk)
    assert o[e] < o[d]
    assert o[d] < o[b] or o[d] < o[c]


def test_prefer_deep(abcde):
    """
        c
        |
    y   b
    |   |
    x   a

    Prefer longer chains first so we should start with c
    """
    a, b, c, d, e = abcde
    dsk = {a: 1, b: (f, a), c: (f, b),
           'x': 1, 'y': (f, 'x')}

    o = order(dsk)
    assert o == {c: 0, b: 1, a: 2, 'y': 3, 'x': 4}


def test_stacklimit(abcde):
    dsk = dict(('x%s' % (i + 1), (inc, 'x%s' % i)) for i in range(10000))
    dependencies, dependents = get_deps(dsk)
    ndependencies(dependencies, dependents)


def test_break_ties_by_str(abcde):
    a, b, c, d, e = abcde
    dsk = {('x', i): (inc, i) for i in range(10)}
    x_keys = sorted(dsk)
    dsk['y'] = list(x_keys)

    o = order(dsk)
    expected = {'y': 0}
    expected.update({k: i + 1 for i, k in enumerate(x_keys)})

    assert o == expected


def test_order_doesnt_fail_on_mixed_type_keys(abcde):
    order({'x': (inc, 1),
           ('y', 0): (inc, 2),
           'z': (add, 'x', ('y', 0))})


def test_gh_3055():
    da = pytest.importorskip('dask.array')
    A, B = 20, 99
    x = da.random.normal(size=(A, B), chunks=(1, None))
    for _ in range(2):
        y = (x[:, None, :] * x[:, :, None]).cumsum(axis=0)
        x = x.cumsum(axis=0)
    w = (y * x[:, None]).sum(axis=(1,2))

    dsk = dict(w.__dask_graph__())
    o = order(dsk)
    L = [o[k] for k in w.__dask_keys__()]
    assert sorted(L) == L[::-1]


def test_type_comparisions_ok(abcde):
    a, b, c, d, e = abcde
    dsk = {a: 1, (a, 1): 2, (a, b, 1): 3}
    order(dsk)  # this doesn't err
