from __future__ import annotations

import typing
from dataclasses import dataclass

__all__ = (
    "And",
    "Field",
    "Not",
    "Or",
    "PythonQEvaluator",
    "Q",
    "QEvaluator",
    "QTranslator",
)


@dataclass(frozen=True, slots=True)
class Q:
    """
    Base class for query expressions.

    Q objects form a small query expression language that is independent
    of the underlying data source or query backend.

    Query expressions can be composed using boolean operators:

        - ``&`` creates an AND expression.
        - ``|`` creates an OR expression.
        - ``~`` creates a NOT expression.

    Field comparisons are created using :meth:`field`.

    Example:

        query = (
            Q.field("uid", "eq", 123)
            & Q.field("closed", "eq", False)
        )

    The resulting expression can be evaluated against Python objects or
    translated into a backend-specific query using an appropriate
    evaluator or translator.

    Q expressions are immutable and hashable.
    """

    @classmethod
    def field(
        cls,
        name: str,
        op: typing.Literal["eq", "neq", "gt", "gte", "lt", "lte", "isnull"],
        value: typing.Any,
    ) -> Field:
        """
        Create a field comparison expression.

        The expression compares an object's attribute identified by
        ``name`` with ``value`` using the specified operator.

        Supported operators:

            - ``eq``:
                Equal to.

            - ``neq``:
                Not equal to.

            - ``gt``:
                Greater than.

            - ``gte``:
                Greater than or equal to.

            - ``lt``:
                Less than.

            - ``lte``:
                Less than or equal to.

            - ``isnull``:
                Checks whether the field is ``None``.

        Example:

            query = Q.field("profit", "gt", 100)

        :param name:
            Name of the field or object attribute.
        :param op:
            Comparison operator.
        :param value:
            Value to compare against.

        :return:
            A field comparison expression.
        """
        return Field(name, op, value)

    def __and__(self, other: Q) -> Q:
        """
        Combine two expressions using logical AND.

        Example:

            query = (
                Q.field("asset", "eq", "EURUSD")
                & Q.field("closed", "eq", False)
            )

        :param other:
            Expression to combine with this expression.

        :return:
            An AND expression containing both operands.
        """
        return And(self, other)

    def __or__(self, other: Q) -> Q:
        """
        Combine two expressions using logical OR.

        Example:

            query = (
                Q.field("profit", "gt", 100)
                | Q.field("profit", "lt", -100)
            )

        :param other:
            Expression to combine with this expression.

        :return:
            An OR expression containing both operands.
        """
        return Or(self, other)

    def __invert__(self) -> Q:
        """
        Negate this expression using logical NOT.

        Example:

            query = ~Q.field("closed", "eq", True)

        :return:
            A NOT expression containing this expression.
        """
        return Not(self)


_FIELD_OPERATORS = {
    "eq": "==",
    "neq": "!=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


@dataclass(frozen=True, slots=True)
class Field(Q):
    """
    Field comparison expression.

    Represents a comparison between an object field and a value.

    This is a leaf node of the query expression tree.

    Example:

        query = Q.field("balance", "gte", 100)

    :ivar name:
        Name of the field to inspect.
    :ivar op:
        Comparison operator.
    :ivar value:
        Value used for comparison.
    """

    name: str
    op: typing.Literal[
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "isnull",
    ]
    value: typing.Any

    def __repr__(self) -> str:
        if self.op in _FIELD_OPERATORS:
            return f"Q({self.name} {_FIELD_OPERATORS[self.op]} {self.value})"
        if self.op == "isnull" and self.value is True:
            return f"Q({self.name} is None)"
        if self.op == "isnull" and self.value is False:
            return f"Q({self.name} is not None)"
        return super().__repr__()


@dataclass(frozen=True, slots=True)
class And(Q):
    """
    Logical AND expression.

    Evaluates to true when both child expressions evaluate to true.

    Example:

        query = (
            Q.field("asset", "eq", "EURUSD")
            & Q.field("closed", "eq", False)
        )

    :ivar left:
        Left-hand expression.
    :ivar right:
        Right-hand expression.
    """

    left: Q
    right: Q

    def __repr__(self) -> str:
        return f"Q({self.left!r} AND {self.right!r})"


@dataclass(frozen=True, slots=True)
class Or(Q):
    """
    Logical OR expression.

    Evaluates to true when at least one of the child expressions
    evaluates to true.

    Example:

        query = (
            Q.field("profit", "gt", 100)
            | Q.field("profit", "lt", -100)
        )

    :ivar left:
        Left-hand expression.
    :ivar right:
        Right-hand expression.
    """

    left: Q
    right: Q

    def __repr__(self) -> str:
        return f"Q({self.left!r} OR {self.right!r})"


@dataclass(frozen=True, slots=True)
class Not(Q):
    """
    Logical NOT expression.

    Negates the result of the wrapped expression.

    Example:

        query = ~Q.field("closed", "eq", True)

    :ivar expression:
        Expression whose result should be negated.
    """

    expression: Q

    def __repr__(self) -> str:
        return f"Q(NOT {self.expression!r})"


class QEvaluator(typing.Protocol):
    """
    Protocol for evaluating query expressions against objects.

    Evaluators execute a Q expression directly against a data object
    instead of translating it into another query language.
    """

    def evaluate(self, query: Q, obj: typing.Any) -> bool:
        """
        Evaluate a query expression against an object.

        :param query:
            Query expression to evaluate.
        :param obj:
            Object against which the expression is evaluated.

        :return:
            ``True`` if the object matches the query, otherwise ``False``.
        """
        ...


class QTranslator[T](typing.Protocol):
    """
    Protocol for translating query expressions into a backend query.

    Translators convert the backend-independent Q expression tree into
    a query representation understood by a specific storage engine,
    ORM, database, or external service.

    Examples of possible implementations include:

        - Tortoise ORM translator;
        - SQL translator;
        - Elasticsearch translator;
        - in-memory query translator.
    """

    def translate(self, query: Q) -> T:
        """
        Translate a query expression into a backend-specific query.

        :param query:
            Query expression to translate.

        :return:
            Backend-specific query representation.
        """
        ...


class PythonQEvaluator(QEvaluator):
    """
    Evaluate Q expressions directly against Python objects.

    Field values are retrieved using :func:`getattr` and compared using
    the operator specified by the corresponding :class:`Field` expression.

    Example:

        query = (
            Q.field("uid", "eq", 123)
            & Q.field("closed", "eq", False)
        )

        evaluator = PythonQEvaluator()

        if evaluator.evaluate(query, deal):
            print("Deal matches query")
    """

    def evaluate(self, query: Q, obj: typing.Any) -> bool:  # noqa: PLR0911
        """
        Evaluate a query expression against a Python object.

        The expression tree is recursively evaluated from its root.

        :param query:
            Query expression to evaluate.
        :param obj:
            Object whose attributes are evaluated.

        :raises TypeError:
            If an unsupported query expression is encountered.

        :return:
            ``True`` if the object matches the query, otherwise ``False``.
        """

        match query:
            case Field(name, op, value):
                actual = getattr(obj, name)

                match op:
                    case "eq":
                        return actual == value
                    case "neq":
                        return actual != value
                    case "gt":
                        return actual > value
                    case "gte":
                        return actual >= value
                    case "lt":
                        return actual < value
                    case "lte":
                        return actual <= value
                    case "isnull":
                        return (actual is None) == value

            case And(left, right):
                return self.evaluate(left, obj) and self.evaluate(right, obj)

            case Or(left, right):
                return self.evaluate(left, obj) or self.evaluate(right, obj)

            case Not(expression):
                return not self.evaluate(expression, obj)

        raise TypeError(query)
