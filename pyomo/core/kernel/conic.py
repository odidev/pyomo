#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________
"""Various conic constraint implementations."""

import math

from pyomo.core.expr.current import (value,
                                     exp)
from pyomo.core.kernel.variable import IVariable
from pyomo.core.kernel.constraint import IConstraint

class _ConicBase(IConstraint):
    """Base class for a few conic constraints that
    implements some shared functionality. Derived classes
    are expected to declare any necessary slots."""
    _ctype = IConstraint
    _linear_canonical_form = False
    __slots__ = ()

    def __init__(self):
        self._parent = None
        self._storage_key = None
        self._active = True
        # the body expression is only built if necessary
        # (i.e., when someone asks for it via the body
        # property method)
        self._body = None

    def _body_function(self, *args):
        """A function that defines the body expression"""
        raise NotImplementedError     #pragma:nocover

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        raise NotImplementedError     #pragma:nocover

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        raise NotImplementedError     #pragma:nocover

    #
    # Define the IConstraint abstract methods
    #

    @property
    def body(self):
        """The body of the constraint"""
        if self._body is None:
            self._body = self._body_function(
                *self._body_function_variables(values=False))
        return self._body

    @property
    def lb(self):
        """The lower bound of the constraint"""
        return None

    @property
    def ub(self):
        """The upper bound of the constraint"""
        return 0.0

    @property
    def rhs(self):
        """The right-hand side of the constraint"""
        raise ValueError(
            "The rhs property can not be read because this "
            "is not an equality constraint")

    @property
    def equality(self):
        return False

    #
    # Override a the default __call__ method on IConstraint
    # to avoid building the body expression, if possible
    #

    def __call__(self, exception=True):
        try:
            # we wrap the result with value(...) as the
            # alpha term used by some of the constraints
            # may be a parameter
            return value(self._body_function(
                *self._body_function_variables(values=True)))
        except (ValueError, TypeError):
            if exception:
                raise ValueError("one or more terms "
                                 "could not be evaluated")
            return None

class quadratic(_ConicBase):
    """A quadratic conic constraint of the form:

        x[0]^2 + ... + x[n-1]^2 <= r^2,

    which is recognized as convex for r >= 0.

    Parameters
    ----------
    x : list[:class:`variable`]
        An iterable of variables.
    r : :class:`variable`
        A variable.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x",
                 "_r",
                 "__weakref__")
    def __init__(self, x, r):
        super(quadratic, self).__init__()
        self._x = tuple(x)
        self._r = r
        assert all(isinstance(xi, IVariable)
                   for xi in self._x)
        assert isinstance(self._r, IVariable)

    @property
    def x(self):
        return self._x

    @property
    def r(self):
        return self._r

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x, r):
        """A function that defines the body expression"""
        return sum(xi**2 for xi in x) - r**2

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x, self.r
        else:
            return tuple(xi.value for xi in self.x), self.r.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        return (relax or \
                (self.r.is_continuous() and \
                 all(xi.is_continuous() for xi in self.x))) and \
            (self.r.has_lb() and value(self.r.lb) >= 0)

class rotated_quadratic(_ConicBase):
    """A rotated quadratic conic constraint of the form:

        x[0]^2 + ... + x[n-1]^2 <= 2*r1*r2,

    which is recognized as convex for r1,r2 >= 0.

    Parameters
    ----------
    x : list[:class:`variable`]
        An iterable of variables.
    r1 : :class:`variable`
        A variable.
    r2 : :class:`variable`
        A variable.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x",
                 "_r1",
                 "_r2",
                 "__weakref__")

    def __init__(self, x, r1, r2):
        super(rotated_quadratic, self).__init__()
        self._x = tuple(x)
        self._r1 = r1
        self._r2 = r2
        assert all(isinstance(xi, IVariable)
                   for xi in self._x)
        assert isinstance(self._r1, IVariable)
        assert isinstance(self._r2, IVariable)

    @property
    def x(self):
        return self._x

    @property
    def r1(self):
        return self._r1

    @property
    def r2(self):
        return self._r2

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x, r1, r2):
        """A function that defines the body expression"""
        return sum(xi**2 for xi in x) - 2*r1*r2

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x, self.r1, self.r2
        else:
            return tuple(xi.value for xi in self.x), \
                self.r1.value, self.r2.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        return (relax or \
                (self.r1.is_continuous() and \
                 self.r2.is_continuous() and \
                 all(xi.is_continuous() for xi in self.x))) and \
            (self.r1.has_lb() and value(self.r1.lb) >= 0) and \
            (self.r2.has_lb() and value(self.r2.lb) >= 0)

class primal_exponential(_ConicBase):
    """A primal exponential conic constraint of the form:

        x1*exp(x2/x1) <= r,

    which is recognized as convex for x1,r >= 0.

    Parameters
    ----------
    x1 : :class:`variable`
        A variable.
    x2 : :class:`variable`
        A variable.
    r : :class:`variable`
        A variable.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x1",
                 "_x2",
                 "_r",
                 "__weakref__")

    def __init__(self, x1, x2, r):
        super(primal_exponential, self).__init__()
        self._x1 = x1
        self._x2 = x2
        self._r = r
        assert isinstance(self._x1, IVariable)
        assert isinstance(self._x2, IVariable)
        assert isinstance(self._r, IVariable)

    @property
    def x1(self):
        return self._x1

    @property
    def x2(self):
        return self._x2

    @property
    def r(self):
        return self._r

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x1, x2, r):
        """A function that defines the body expression"""
        return x1*exp(x2/x1) - r

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x1, self.x2, self.r
        else:
            return self.x1.value, self.x2.value, self.r.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        return (relax or \
                (self.x1.is_continuous() and \
                 self.x2.is_continuous() and \
                 self.r.is_continuous())) and \
            (self.x1.has_lb() and value(self.x1.lb) >= 0) and \
            (self.r.has_lb() and value(self.r.lb) >= 0)

class primal_power(_ConicBase):
    """A primal power conic constraint of the form:
       sqrt(x[0]^2 + ... + x[n-1]^2) <= (r1^alpha)*(r2^(1-alpha))

    which is recognized as convex for r1,r2 >= 0
    and 0 < alpha < 1.

    Parameters
    ----------
    x : list[:class:`variable`]
        An iterable of variables.
    r1 : :class:`variable`
        A variable.
    r2 : :class:`variable`
        A variable.
    alpha : float, :class:`parameter`, etc.
        A constant term.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x",
                 "_r1",
                 "_r2",
                 "_alpha",
                 "__weakref__")

    def __init__(self, x, r1, r2, alpha):
        super(primal_power, self).__init__()
        self._x = tuple(x)
        self._r1 = r1
        self._r2 = r2
        self._alpha = alpha
        assert all(isinstance(xi, IVariable)
                   for xi in self._x)
        assert isinstance(self._r1, IVariable)
        assert isinstance(self._r2, IVariable)

    @property
    def x(self):
        return self._x

    @property
    def r1(self):
        return self._r1

    @property
    def r2(self):
        return self._r2

    @property
    def alpha(self):
        return self._alpha

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x, r1, r2):
        """A function that defines the body expression"""
        alpha = self.alpha
        return (sum(xi**2 for xi in x)**0.5) - \
            (r1**alpha) * \
            (r2**(1-alpha))

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x, self.r1, self.r2
        else:
            return tuple(xi.value for xi in self.x), \
                self.r1.value, self.r2.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        alpha = value(self.alpha, exception=False)
        return (relax or \
                (self.r1.is_continuous() and \
                 self.r2.is_continuous() and \
                 all(xi.is_continuous() for xi in self.x))) and \
            (self.r1.has_lb() and value(self.r1.lb) >= 0) and \
            (self.r2.has_lb() and value(self.r2.lb) >= 0) and \
            ((alpha is not None) and (0 < alpha < 1))

class dual_exponential(_ConicBase):
    """A dual exponential conic constraint of the form:

        -(x2/e)*exp(x1/x2) <= r

    which is recognized as convex for x2 <= 0 and r >= 0.
    Note that e in the expression is Euler's constant.

    Parameters
    ----------
    x1 : :class:`variable`
        A variable.
    x2 : :class:`variable`
        A variable.
    r : :class:`variable`
        A variable.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x1",
                 "_x2",
                 "_r",
                 "__weakref__")

    def __init__(self, x1, x2, r):
        super(dual_exponential, self).__init__()
        self._x1 = x1
        self._x2 = x2
        self._r = r
        assert isinstance(self._x1, IVariable)
        assert isinstance(self._x2, IVariable)
        assert isinstance(self._r, IVariable)

    @property
    def x1(self):
        return self._x1

    @property
    def x2(self):
        return self._x2

    @property
    def r(self):
        return self._r

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x1, x2, r):
        """A function that defines the body expression"""
        return -(x2/math.e)*exp(x1/x2) - r

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x1, self.x2, self.r
        else:
            return self.x1.value, self.x2.value, self.r.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        return (relax or \
                (self.x1.is_continuous() and \
                 self.x2.is_continuous() and \
                 self.r.is_continuous())) and \
            (self.x2.has_ub() and value(self.x2.ub) <= 0) and \
            (self.r.has_lb() and value(self.r.lb) >= 0)

class dual_power(_ConicBase):
    """A dual power conic constraint of the form:

        sqrt(x[0]^2 + ... + x[n-1]^2) <= ((r1/alpha)^alpha) * \
                                         ((r2/(1-alpha))^(1-alpha))

    which is recognized as convex for r1,r2 >= 0
    and 0 < alpha < 1.

    Parameters
    ----------
    x : list[:class:`variable`]
        An iterable of variables.
    r1 : :class:`variable`
        A variable.
    r2 : :class:`variable`
        A variable.
    alpha : float, :class:`parameter`, etc.
        A constant term.
    """
    __slots__ = ("_parent",
                 "_storage_key",
                 "_active",
                 "_body",
                 "_x",
                 "_r1",
                 "_r2",
                 "_alpha",
                 "__weakref__")

    def __init__(self, x, r1, r2, alpha):
        super(dual_power, self).__init__()
        self._x = tuple(x)
        self._r1 = r1
        self._r2 = r2
        self._alpha = alpha
        assert all(isinstance(xi, IVariable)
                   for xi in self._x)
        assert isinstance(self._r1, IVariable)
        assert isinstance(self._r2, IVariable)

    @property
    def x(self):
        return self._x

    @property
    def r1(self):
        return self._r1

    @property
    def r2(self):
        return self._r2

    @property
    def alpha(self):
        return self._alpha

    #
    # Define the _ConicBase abstract methods
    #

    def _body_function(self, x, r1, r2):
        """A function that defines the body expression"""
        alpha = self.alpha
        return (sum(xi**2 for xi in x)**0.5) - \
            ((r1/alpha)**alpha) * \
            ((r2/(1-alpha))**(1-alpha))

    def _body_function_variables(self, values=False):
        """Returns variables in the order they should be
        passed to the body function. If values is True, then
        return the current value of each variable in place
        of the variables themselves."""
        if not values:
            return self.x, self.r1, self.r2
        else:
            return tuple(xi.value for xi in self.x),\
                self.r1.value, self.r2.value

    def check_convexity_conditions(self, relax=False):
        """Returns True if all convexity conditions for the
        conic constraint are satisfied. If relax is True,
        then variable domains are ignored and it is assumed
        that all variables are continuous."""
        alpha = value(self.alpha, exception=False)
        return (relax or \
                (self.r1.is_continuous() and \
                 self.r2.is_continuous() and \
                 all(xi.is_continuous() for xi in self.x))) and \
            (self.r1.has_lb() and value(self.r1.lb) >= 0) and \
            (self.r2.has_lb() and value(self.r2.lb) >= 0) and \
            ((alpha is not None) and (0 < alpha < 1))
