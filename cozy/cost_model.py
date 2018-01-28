from collections import OrderedDict
from functools import total_ordering, lru_cache
import itertools

from cozy.common import typechecked, partition
from cozy.target_syntax import *
from cozy.syntax_tools import BottomUpExplorer, pprint, equal, fresh_var, mk_lambda, free_vars, subst, alpha_equivalent
from cozy.typecheck import is_collection
from cozy.pools import RUNTIME_POOL, STATE_POOL
from cozy.solver import valid, satisfiable, REAL, SolverReportedUnknown, IncrementalSolver
from cozy.evaluation import eval
from cozy.opts import Option

assume_large_cardinalities = Option("assume-large-cardinalities", bool, True)
simple_cost_model = Option("simple-cost-model", bool, False)

# In principle these settings are supposed to improve performance; in practice,
# they do not.
incremental = False
use_indicators = False

class CostModel(object):
    def cost(self, e, pool):
        raise NotImplementedError()
    def is_monotonic(self):
        raise NotImplementedError()

@lru_cache(maxsize=2**16)
# @typechecked
def cardinality_le(c1 : Exp, c2 : Exp, assumptions : Exp = T, as_f : bool = False, solver : IncrementalSolver = IncrementalSolver()) -> bool:
    """
    Is |c1| <= |c2|?
    Yes, iff there are no v such that v occurs more times in c2 than in c1.
    """
    assert c1.type == c2.type
    if True:
        f = EBinOp(ELen(c1), "<=", ELen(c2)).with_type(BOOL)
    else:
        # Oh heck.
        # This isn't actually very smart if:
        #   x = [y]
        #   a = Filter (!= y) b
        # This method can't prove that |x| <= |a|, even though |a| is likely huge
        v = fresh_var(c1.type.t)
        f = EBinOp(ECountIn(v, c1), "<=", ECountIn(v, c2)).with_type(BOOL)
    if as_f:
        return f
    return solver.valid(EImplies(assumptions, f))

@lru_cache(maxsize=2**16)
@typechecked
def cardinality_le_implies_lt(c1 : Exp, c2 : Exp, assumptions : Exp) -> bool:
    return False # disabled for performance
    assert c1.type == c2.type
    v = fresh_var(c1.type.t)
    return satisfiable(EAll((assumptions, EIn(v, c2), ENot(EIn(v, c1)))))

class Cost(object):

    WORSE = "worse"
    BETTER = "better"
    UNORDERED = "unordered"

    def __init__(self,
            e             : Exp,
            pool          : int,
            formula       : Exp,
            secondary     : float          = 0.0,
            assumptions   : Exp            = T,
            cardinalities : { EVar : Exp } = None):
        assert formula.type == INT
        self.e = e
        self.pool = pool
        self.formula = formula
        self.secondary = secondary
        assert all(v.type == INT for v in free_vars(assumptions))
        self.assumptions = assumptions
        self.cardinalities = cardinalities or { }

    def order_cardinalities(self, other, assumptions : Exp):
        if incremental:
            s = IncrementalSolver()

        cardinalities = OrderedDict()
        cardinalities.update(self.cardinalities)
        cardinalities.update(other.cardinalities)

        conds = []
        res = []
        for (v1, c1) in cardinalities.items():
            res.append(EBinOp(v1, ">=", ZERO).with_type(BOOL))
            for (v2, c2) in cardinalities.items():
                if v1 == v2 or c1.type != c2.type:
                    continue
                if alpha_equivalent(c1, c2):
                    res.append(EEq(v1, v2))
                    continue

                if incremental and use_indicators:
                    conds.append((v1, v2, fresh_var(BOOL), cardinality_le(c1, c2, as_f=True)))
                else:
                    if incremental:
                        le = cardinality_le(c1, c2, solver=s)
                    else:
                        le = cardinality_le(c1, c2, assumptions=assumptions)
                    if le:
                        if cardinality_le_implies_lt(c1, c2, assumptions):
                            res.append(EBinOp(v1, "<", v2).with_type(BOOL))
                        else:
                            res.append(EBinOp(v1, "<=", v2).with_type(BOOL))

        if incremental and use_indicators:
            s.add_assumption(EAll(
                [assumptions] +
                [EEq(indicator, f) for (v1, v2, indicator, f) in conds]))
            for (v1, v2, indicator, f) in conds:
                if s.valid(indicator):
                    res.append(EBinOp(v1, "<=", v2).with_type(BOOL))

        # print("cards: {}".format(pprint(EAll(res))))
        return EAll(res)

    @typechecked
    def always(self, op, other, assumptions : Exp = T, cards = None, **kwargs) -> bool:
        """
        Partial order on costs subject to assumptions.
        """
        if cards is None:
            cards = self.order_cardinalities(other, assumptions)
        if isinstance(self.formula, ENum) and isinstance(other.formula, ENum):
            return eval(EBinOp(self.formula, op, other.formula).with_type(BOOL), env={})
        f = EImplies(
            EAll((self.assumptions, other.assumptions, cards)),
            EBinOp(self.formula, op, other.formula).with_type(BOOL))
        try:
            return valid(f, logic="QF_LIA", timeout=1, **kwargs)
        except SolverReportedUnknown:
            # If we accidentally made an unsolveable integer arithmetic formula,
            # then try again with real numbers. This will admit some models that
            # are not possible (since bags must have integer cardinalities), but
            # returning false is always a safe move here, so it's fine.
            print("Warning: not able to solve {}".format(pprint(f)))
            f = subst(f, { v.id : EVar(v.id).with_type(REAL) for v in free_vars(cards) })
            try:
                return valid(f, logic="QF_NRA", timeout=5, **kwargs)
            except SolverReportedUnknown:
                print("Giving up!")
                return False

    def compare_to(self, other, assumptions : Exp = T) -> bool:
        cards = self.order_cardinalities(other, assumptions)
        o1 = self.always("<=", other, cards=cards)
        o2 = other.always("<=", self, cards=cards)

        if o1 and not o2:
            return Cost.BETTER
        elif o2 and not o1:
            return Cost.WORSE
        elif not o1 and not o2:
            return Cost.UNORDERED
        else:
            return (Cost.WORSE if self.secondary > other.secondary else
                    Cost.BETTER if self.secondary < other.secondary else
                    Cost.UNORDERED)

    def always_worse_than(self, other, assumptions : Exp = T, cards : Exp = None) -> bool:
        # it is NOT possible that `self` takes less time than `other`
        return self.always(">", other, assumptions, cards)

    def always_better_than(self, other, assumptions : Exp = T, cards : Exp = None) -> bool:
        # it is NOT possible that `self` takes more time than `other`
        return self.always("<", other, assumptions, cards)

    def sometimes_worse_than(self, other, assumptions : Exp = T, cards : Exp = None) -> bool:
        # it is possible that `self` takes more time than `other`
        return not self.always("<=", other, assumptions, cards)

    def sometimes_better_than(self, other, assumptions : Exp = T, cards : Exp = None) -> bool:
        # it is possible that `self` takes less time than `other`
        return not self.always(">=", other, assumptions, cards)

    def __str__(self):
        return "cost[{} subject to {}, {}]".format(
            pprint(self.formula),
            pprint(self.assumptions),
            ", ".join(pprint(EEq(v, EUnaryOp(UOp.Length, e))) for v, e in self.cardinalities.items()))

    def __repr__(self):
        return "Cost({!r}, assumptions={!r}, cardinalities={!r})".format(
            self.formula,
            self.assumptions,
            self.cardinalities)

def debug_comparison(e1, c1, e2, c2):
    print("-" * 20)
    print("comparing costs...")
    print("  e1 = {}".format(pprint(e1)))
    print("  c1 = {}".format(c1))
    print("  e2 = {}".format(pprint(e2)))
    print("  c2 = {}".format(c2))
    print("  c1 compare_to c2 = {}".format(c1.compare_to(c2)))
    print("  c2 compare_to c1 = {}".format(c2.compare_to(c1)))
    print("secondaries...")
    print("  s1 = {}".format(c1.secondary))
    print("  s2 = {}".format(c2.secondary))
    print("variable meanings...")
    for v, e in itertools.chain(c1.cardinalities.items(), c2.cardinalities.items()):
        print("  {v} = len {e}".format(v=pprint(v), e=pprint(e)))
    print("explicit assumptions...")
    print("  {}".format(pprint(c1.assumptions)))
    print("  {}".format(pprint(c2.assumptions)))
    print("joint orderings...")
    cards = c1.order_cardinalities(c2, assumptions=T)
    print("  {}".format(pprint(cards)))
    for op in ("<=", "<", ">", ">="):
        print("c1 always {} c2?".format(op))
        x = []
        res = c1.always(op, c2, assumptions=T, cards=cards, model_callback=lambda m: x.append(m))
        if res:
            print("  YES")
        elif not x:
            print("  NO (no model!?)")
        else:
            print("  NO: {}".format(x[0]))
            print("  c1 = {}".format(eval(c1.formula, env=x[0])))
            print("  c2 = {}".format(eval(c2.formula, env=x[0])))

Cost.ZERO = Cost(None, None, ZERO)

def break_sum(e):
    if isinstance(e, EBinOp) and e.op == "+":
        yield from break_sum(e.e1)
        yield from break_sum(e.e2)
    else:
        yield e

def ESum(es):
    es = [e for x in es for e in break_sum(x) if e != ZERO]
    if not es:
        return ZERO
    nums, nonnums = partition(es, lambda e: isinstance(e, ENum))
    es = nonnums
    if nums:
        es.append(ENum(sum(n.val for n in nums)).with_type(INT))
    return build_balanced_tree(INT, "+", es)

# Some kinds of expressions have a massive penalty associated with them if they
# appear at runtime.
EXTREME_COST = ENum(1000).with_type(INT)
MILD_PENALTY = ENum(  10).with_type(INT)
TWO          = ENum(   2).with_type(INT)

class CompositeCostModel(CostModel, BottomUpExplorer):
    def __init__(self):
        super().__init__()
    def __repr__(self):
        return "CompositeCostModel()"
    def cardinality(self, e : Exp, plus_one=False) -> Exp:
        # if plus_one:
        #     return ESum((self.cardinality(e, plus_one=False), ONE))
        if isinstance(e, EEmptyList):
            return ZERO
        if isinstance(e, ESingleton):
            return ONE
        if isinstance(e, EBinOp) and e.op == "+":
            return ESum((self.cardinality(e.e1), self.cardinality(e.e2)))
        if isinstance(e, EMap):
            return self.cardinality(e.e)
        if isinstance(e, EStateVar):
            return self.cardinality(e.e)
        if e in self.cardinalities:
            return self.cardinalities[e]
        else:
            v = fresh_var(INT)
            self.cardinalities[e] = v
            if isinstance(e, EFilter):
                cc = self.cardinality(e.e)
                self.assumptions.append(EBinOp(v, "<=", cc).with_type(BOOL))
                # heuristic: (xs) large implies (filter_p xs) large
                self.assumptions.append(EBinOp(
                    EBinOp(v,  "*", ENum(5).with_type(INT)).with_type(INT), ">=",
                    EBinOp(cc, "*", ENum(4).with_type(INT)).with_type(INT)).with_type(BOOL))
            if isinstance(e, EUnaryOp) and e.op == UOp.Distinct:
                cc = self.cardinality(e.e)
                self.assumptions.append(EBinOp(v, "<=", cc).with_type(BOOL))
                # self.assumptions.append(EImplies(EGt(cc, ZERO), EGt(v, ZERO)))
                # heuristic: (xs) large implies (distinct xs) large
                self.assumptions.append(EBinOp(
                    EBinOp(v,  "*", ENum(5).with_type(INT)).with_type(INT), ">=",
                    EBinOp(cc, "*", ENum(4).with_type(INT)).with_type(INT)).with_type(BOOL))
            if isinstance(e, EBinOp) and e.op == "-":
                self.assumptions.append(EBinOp(v, "<=", self.cardinality(e.e1)).with_type(BOOL))
            if isinstance(e, ECond):
                self.assumptions.append(EAny([EEq(v, self.cardinality(e.then_branch)), EEq(v, self.cardinality(e.else_branch))]))
            return v
    def statecost(self, e : Exp) -> float:
        return e.size() / 100
    def sizeof(self, e : Exp) -> Exp:
        """
        The cost of storing `e` on the data structure
        """
        if is_collection(e.type):
            return self.cardinality(e, plus_one=True)
        # if isinstance(e.type, TMap):
        #     return EBinOp(
        #         self.cardinality(EMapKeys(e).with_type(TBag(e.type.k))),
        #         "*",
        #         self.sizeof(EMapGet(e, fresh_var(e.type.k)).with_type(e.type.v))).with_type(INT)
        return ONE
    def visit_EStateVar(self, e):
        self.secondaries += self.statecost(e.e)
        return ESum([ONE, self.sizeof(e.e)])
    def visit_EUnaryOp(self, e):
        costs = [ONE, self.visit(e.e)]
        if e.op in (UOp.Sum, UOp.Distinct, UOp.AreUnique, UOp.All, UOp.Any, UOp.Length):
            costs.append(self.cardinality(e.e))
        return ESum(costs)
    def visit_EBinOp(self, e):
        c1 = self.visit(e.e1)
        c2 = self.visit(e.e2)
        costs = [ONE, c1, c2]
        if e.op == BOp.In:
            costs.append(self.cardinality(e.e2))
        elif e.op == "==" and is_collection(e.e1.type):
            costs.append(EXTREME_COST)
            costs.append(self.cardinality(e.e1))
            costs.append(self.cardinality(e.e2))
        elif e.op == "-" and is_collection(e.type):
            costs.append(EXTREME_COST)
            costs.append(self.cardinality(e.e1))
            costs.append(self.cardinality(e.e2))
        return ESum(costs)
    def visit_ELambda(self, e):
        # avoid name collisions with fresh_var
        return self.visit(e.apply_to(fresh_var(e.arg.type)))
    def visit_EMapGet(self, e):
        # mild penalty here because we want "x.f" < "map.get(x)"
        return ESum((MILD_PENALTY, self.visit(e.map), self.visit(e.key)))
    def visit_EMakeMap2(self, e):
        return ESum((EXTREME_COST, self.visit(e.e), EBinOp(self.cardinality(e.e, plus_one=True), "*", self.visit(e.value)).with_type(INT)))
    def visit_EFilter(self, e):
        return ESum((TWO, self.visit(e.e), EBinOp(self.cardinality(e.e, plus_one=True), "*", self.visit(e.p)).with_type(INT)))
    def visit_EFlatMap(self, e):
        return self.visit(EMap(e.e, e.f))
    def visit_EMap(self, e):
        return ESum((TWO, self.visit(e.e), EBinOp(self.cardinality(e.e, plus_one=True), "*", self.visit(e.f)).with_type(INT)))
    def visit_EArgMin(self, e):
        return ESum((TWO, self.visit(e.e), EBinOp(self.cardinality(e.e, plus_one=True), "*", self.visit(e.f)).with_type(INT)))
    def visit_EArgMax(self, e):
        return ESum((TWO, self.visit(e.e), EBinOp(self.cardinality(e.e, plus_one=True), "*", self.visit(e.f)).with_type(INT)))
    def visit_EDropFront(self, e):
        return ESum((MILD_PENALTY, self.visit(e.e), self.cardinality(e.e, plus_one=True).with_type(INT)))
    def visit_EDropBack(self, e):
        return ESum((MILD_PENALTY, self.visit(e.e), self.cardinality(e.e, plus_one=True).with_type(INT)))
    def join(self, x, child_costs):
        if isinstance(x, list) or isinstance(x, tuple):
            return ESum(child_costs)
        if not isinstance(x, Exp):
            return ZERO
        return ESum(itertools.chain((ONE,), child_costs))
    def is_monotonic(self):
        return False
    def cost(self, e, pool):
        if simple_cost_model.value:
            return Cost(e, pool, ZERO, secondary=e.size())
        if pool == RUNTIME_POOL:
            self.cardinalities = OrderedDict()
            self.assumptions = []
            min_cardinality = ENum(1000).with_type(INT)
            if assume_large_cardinalities.value:
                for v in free_vars(e):
                    if is_collection(v.type):
                        self.assumptions.append(EBinOp(self.cardinality(v), ">", min_cardinality).with_type(BOOL))
            self.secondaries = 0
            f = self.visit(e)
            invcard = OrderedDict()
            for e, v in self.cardinalities.items():
                invcard[v] = e
            return Cost(e, pool, f, secondary=self.secondaries, assumptions=EAll(self.assumptions), cardinalities=invcard)
        else:
            return Cost(e, pool, ZERO, secondary=self.statecost(e))