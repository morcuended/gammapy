# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Interpolation utilities."""

import html
from itertools import compress
import numpy as np
import scipy.interpolate
from astropy import units as u
from .compat import COPY_IF_NEEDED

__all__ = [
    "interpolate_profile",
    "interpolation_scale",
    "ScaledRegularGridInterpolator",
]

INTERPOLATION_ORDER = {None: 0, "nearest": 0, "linear": 1, "quadratic": 2, "cubic": 3}


class ScaledRegularGridInterpolator:
    """Thin wrapper around `scipy.interpolate.RegularGridInterpolator`.

    The values are scaled before the interpolation and back-scaled after the
    interpolation.

    Dimensions of length 1 are ignored in the interpolation of the data.

    Parameters
    ----------
    points : tuple of `~numpy.ndarray` or `~astropy.units.Quantity`
        Tuple of points passed to `~scipy.interpolate.RegularGridInterpolator`.
    values : `~numpy.ndarray`
        Values passed to `~scipy.interpolate.RegularGridInterpolator`.
    points_scale : tuple of str
        Interpolation scale used for the points.
    values_scale : {'lin', 'log', 'sqrt'}
        Interpolation scaling applied to values. If the values vary over many magnitudes
        a 'log' scaling is recommended.
    axis : int or None
        Axis along which to interpolate.
    method : {"linear", "nearest"}
        Default interpolation method. Can be overwritten when calling the
        `ScaledRegularGridInterpolator`.
    **kwargs : dict
        Keyword arguments passed to `~scipy.interpolate.RegularGridInterpolator`.
    """

    def __init__(
        self,
        points,
        values,
        points_scale=None,
        values_scale="lin",
        extrapolate=True,
        axis=None,
        **kwargs,
    ):
        if points_scale is None:
            points_scale = ["lin"] * len(points)

        self.scale_points = [interpolation_scale(scale) for scale in points_scale]
        self.scale = interpolation_scale(values_scale)
        self.axis = axis

        self._include_dimensions = [len(p) > 1 for p in points]

        values_scaled = self.scale(values)
        points_scaled = self._scale_points(points=points)

        kwargs.setdefault("bounds_error", False)

        if extrapolate:
            kwargs.setdefault("fill_value", None)

        method = kwargs.get("method", None)

        if not np.any(self._include_dimensions):
            if method != "nearest":
                raise ValueError(
                    "Interpolating scalar values requires using "
                    "method='nearest' explicitly."
                )

        if np.any(self._include_dimensions):
            values_scaled = np.squeeze(values_scaled)

        if axis is None:
            self._interpolate = scipy.interpolate.RegularGridInterpolator(
                points=points_scaled, values=values_scaled, **kwargs
            )
        else:
            self._interpolate = scipy.interpolate.interp1d(
                points_scaled[0], values_scaled, axis=axis
            )

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"

    def _scale_points(self, points):
        points_scaled = [scale(p) for p, scale in zip(points, self.scale_points)]

        if np.any(self._include_dimensions):
            points_scaled = compress(points_scaled, self._include_dimensions)

        return tuple(points_scaled)

    def __call__(self, points, method=None, clip=True, **kwargs):
        """Interpolate data points.

        Parameters
        ----------
        points : tuple of `~numpy.ndarray` or `~astropy.units.Quantity`
            Tuple of coordinate arrays of the form (x_1, x_2, x_3, ...). Arrays are
            broadcast internally.
        method : {None, "linear", "nearest"}
            Linear or nearest neighbour interpolation.
            Default is None, which is `method` defined on init.
        clip : bool
            Clip values at zero after interpolation.
        """
        points = self._scale_points(points=points)

        if self.axis is None:
            points = np.broadcast_arrays(*points)
            points_interp = np.stack([_.flat for _ in points]).T
            values = self._interpolate(points_interp, method, **kwargs)
            values = self.scale.inverse(values.reshape(points[0].shape))
        else:
            values = self._interpolate(points[0])
            values = self.scale.inverse(values)

        if clip:
            values = np.clip(values, 0, np.inf)

        return values


def interpolation_scale(scale="lin"):
    """Interpolation scaling.

    Parameters
    ----------
    scale : {"lin", "log", "sqrt"}
        Choose interpolation scaling.
    """
    if scale in ["lin", "linear"]:
        return LinearScale()
    elif scale == "log":
        return LogScale()
    elif scale == "sqrt":
        return SqrtScale()
    elif scale == "stat-profile":
        return StatProfileScale()
    elif isinstance(scale, InterpolationScale):
        return scale
    else:
        raise ValueError(f"Not a valid value scaling mode: '{scale}'.")


class InterpolationScale:
    """Interpolation scale base class."""

    def __call__(self, values):
        if hasattr(self, "_unit"):
            values = u.Quantity(values, copy=COPY_IF_NEEDED).to_value(self._unit)
        else:
            if isinstance(values, u.Quantity):
                self._unit = values.unit
                values = values.value
        return self._scale(values)

    def _repr_html_(self):
        try:
            return self.to_html()
        except AttributeError:
            return f"<pre>{html.escape(str(self))}</pre>"

    def inverse(self, values):
        values = self._inverse(values)
        if hasattr(self, "_unit"):
            return u.Quantity(values, self._unit, copy=COPY_IF_NEEDED)
        else:
            return values

    def _inverse_deriv(self, values):
        values = self._inverse_derivative(values)
        if hasattr(self, "_unit"):
            return u.Quantity(values, self._unit, copy=COPY_IF_NEEDED)
        else:
            return values


class LogScale(InterpolationScale):
    """Logarithmic scaling."""

    tiny = np.finfo(np.float32).tiny

    def _scale(self, values):
        values = np.clip(values, self.tiny, np.inf)
        return np.log(values)

    @classmethod
    def _inverse(cls, values):
        output = np.exp(values)
        return np.where(abs(output) - cls.tiny <= cls.tiny, 0, output)

    @classmethod
    def _inverse_derivative(cls, values):
        return cls._inverse(values)


class SqrtScale(InterpolationScale):
    """Square root scaling."""

    @staticmethod
    def _scale(values):
        sign = np.sign(values)
        return sign * np.sqrt(sign * values)

    @classmethod
    def _inverse(cls, values):
        return np.power(values, 2)

    @classmethod
    def _inverse_derivative(cls, values):
        return 2.0 * values


class StatProfileScale(InterpolationScale):
    """Square root profile scaling."""

    def __init__(self, axis=0):
        self.axis = axis

    def _scale(self, values):
        values = np.sign(np.gradient(values, axis=self.axis)) * values
        sign = np.sign(values)
        return sign * np.sqrt(sign * values)

    @classmethod
    def _inverse(cls, values):
        return np.power(values, 2)


class LinearScale(InterpolationScale):
    """Linear scaling."""

    @staticmethod
    def _scale(values):
        return values

    @classmethod
    def _inverse(cls, values):
        return values

    @classmethod
    def _inverse_derivative(cls, values):
        return 1.0


def interpolate_profile(x, y, interp_scale="sqrt", extrapolate=False):
    """Helper function to interpolate one-dimensional profiles.

    Parameters
    ----------
    x : `~numpy.ndarray`
        Array of x values.
    y : `~numpy.ndarray`
        Array of y values.
    interp_scale : {"sqrt", "lin"}
        Interpolation scale applied to the profile. If the profile is
        of parabolic shape, a "sqrt" scaling is recommended. In other cases or
        for fine sampled profiles a "lin" can also be used.
        Default is "sqrt".
    extrapolate : bool
        Extrapolate or not if the evaluation value is outside the range of x values.
        Default is False.

    Returns
    -------
    interp : `interp1d`
        Interpolator.
    """
    method_dict = {"sqrt": "quadratic", "lin": "linear"}
    kwargs = dict(kind=method_dict[interp_scale])
    if extrapolate:
        kwargs["bounds_error"] = False
        kwargs["fill_value"] = "extrapolate"
    return scipy.interpolate.interp1d(x, y, **kwargs)
