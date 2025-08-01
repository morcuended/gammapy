# Licensed under a 3-clause BSD style license - see LICENSE.rst
import logging
import numpy as np
from scipy.stats import median_abs_deviation as mad
import astropy.units as u
from astropy.io import fits
from astropy.table import Table
from regions import CircleSkyRegion, RectangleSkyRegion
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import gammapy.datasets.evaluator as meval
from gammapy.data import GTI, PointingMode
from gammapy.irf import EDispKernelMap, EDispMap, PSFKernel, PSFMap, RecoPSFMap
from gammapy.maps import LabelMapAxis, Map, MapAxes, MapAxis, WcsGeom
from gammapy.modeling.models import DatasetModels, FoVBackgroundModel, Models
from gammapy.stats import (
    CashCountsStatistic,
    WStatCountsStatistic,
    get_wstat_mu_bkg,
)
from gammapy.utils.fits import HDULocation, LazyFitsData
from gammapy.utils.random import get_random_state
from gammapy.utils.scripts import make_name, make_path
from gammapy.utils.table import hstack_columns
from .core import Dataset
from .evaluator import MapEvaluator
from .metadata import MapDatasetMetaData
from .utils import get_axes

__all__ = [
    "MapDataset",
    "MapDatasetOnOff",
    "create_empty_map_dataset_from_irfs",
    "create_map_dataset_geoms",
    "create_map_dataset_from_observation",
]

log = logging.getLogger(__name__)


RAD_MAX = 0.66
RAD_AXIS_DEFAULT = MapAxis.from_bounds(
    0, RAD_MAX, nbin=66, node_type="edges", name="rad", unit="deg"
)
MIGRA_AXIS_DEFAULT = MapAxis.from_bounds(
    0.2, 5, nbin=48, node_type="edges", name="migra"
)

BINSZ_IRF_DEFAULT = 0.2 * u.deg

EVALUATION_MODE = "local"
USE_NPRED_CACHE = True


def create_map_dataset_geoms(
    geom,
    energy_axis_true=None,
    migra_axis=None,
    rad_axis=None,
    binsz_irf=BINSZ_IRF_DEFAULT,
    reco_psf=False,
):
    """Create map geometries for a `MapDataset`.

    Parameters
    ----------
    geom : `~gammapy.maps.WcsGeom`
        Reference target geometry with a reconstructed energy axis. It is used for counts and background maps.
        Additional external data axes can be added to support e.g. event types.
    energy_axis_true : `~gammapy.maps.MapAxis`
        True energy axis used for IRF maps.
    migra_axis : `~gammapy.maps.MapAxis`
        If set, this provides the migration axis for the energy dispersion map.
        If not set, an EDispKernelMap is produced instead. Default is None.
    rad_axis : `~gammapy.maps.MapAxis`
        Rad axis for the PSF map.
    binsz_irf : float
        IRF Map pixel size in degrees.
    reco_psf : bool
        Use reconstructed energy for the PSF geometry. Default is False.

    Returns
    -------
    geoms : dict
        Dictionary with map geometries.
    """
    rad_axis = rad_axis or RAD_AXIS_DEFAULT

    if energy_axis_true is not None:
        energy_axis_true.assert_name("energy_true")
    else:
        energy_axis_true = geom.axes["energy"].copy(name="energy_true")

    external_axes = geom.axes.drop("energy")
    geom_image = geom.to_image()
    geom_exposure = geom_image.to_cube(MapAxes([energy_axis_true]) + external_axes)
    geom_irf = geom_image.to_binsz(binsz=binsz_irf)

    if reco_psf:
        geom_psf = geom_irf.to_cube(
            MapAxes([rad_axis, geom.axes["energy"]]) + external_axes
        )
    else:
        geom_psf = geom_irf.to_cube(
            MapAxes([rad_axis, energy_axis_true]) + external_axes
        )

    if migra_axis:
        geom_edisp = geom_irf.to_cube(
            MapAxes([migra_axis, energy_axis_true]) + external_axes
        )
    else:
        geom_edisp = geom_irf.to_cube(
            MapAxes([geom.axes["energy"], energy_axis_true]) + external_axes
        )

    return {
        "geom": geom,
        "geom_exposure": geom_exposure,
        "geom_psf": geom_psf,
        "geom_edisp": geom_edisp,
    }


def _default_energy_axis(observation, energy_bin_per_decade_max=30, position=None):
    # number of bins per decade estimated from the energy resolution
    # such as diff(ereco.edges)/ereco.center ~ min(eres)

    if isinstance(observation.psf, PSFMap):
        etrue = observation.psf.psf_map.geom.axes[observation.psf.energy_name]
        if isinstance(observation.edisp, EDispKernelMap):
            ekern = observation.edisp.get_edisp_kernel(
                energy_axis=None, position=position
            )
        if isinstance(observation.edisp, EDispMap):
            ekern = observation.edisp.get_edisp_kernel(
                energy_axis=etrue.rename("energy"), position=position
            )
        eres = ekern.get_resolution(etrue.center)
    elif hasattr(observation.psf, "axes"):
        etrue = observation.psf.axes[0]  # only where psf is defined
        if position:
            offset = observation.pointing.fixed_icrs.separation(position)
        else:
            offset = 0 * u.deg
        ekern = observation.edisp.to_edisp_kernel(offset)
        eres = ekern.get_resolution(etrue.center)

    eres = eres[np.isfinite(eres) & (eres > 0.0)]
    if eres.size > 0:
        # remove outliers
        beyond_mad = np.median(eres) - mad(eres) * eres.unit
        eres[eres < beyond_mad] = np.nan
        nbin_per_decade = np.nan_to_num(
            int(np.rint(2.0 / np.nanmin(eres.value))), nan=np.inf
        )
        nbin_per_decade = np.minimum(nbin_per_decade, energy_bin_per_decade_max)
    else:
        nbin_per_decade = energy_bin_per_decade_max

    energy_axis_true = MapAxis.from_energy_bounds(
        etrue.edges[0],
        etrue.edges[-1],
        nbin=nbin_per_decade,
        per_decade=True,
        name="energy_true",
    )
    if hasattr(observation, "bkg") and observation.bkg:
        ereco = observation.bkg.axes["energy"]
        energy_axis = MapAxis.from_energy_bounds(
            ereco.edges[0],
            ereco.edges[-1],
            nbin=nbin_per_decade,
            per_decade=True,
            name="energy",
        )
    else:
        energy_axis = energy_axis_true.rename("energy")

    return energy_axis, energy_axis_true


def _default_binsz(observation, spatial_bin_size_min=0.01 * u.deg):
    # bin size estimated from the minimal r68 of the psf
    if isinstance(observation.psf, PSFMap):
        energy_axis = observation.psf.psf_map.geom.axes[observation.psf.energy_name]
        psf_r68 = observation.psf.containment_radius(0.68, energy_axis.edges)
    elif hasattr(observation.psf, "axes"):
        etrue = observation.psf.axes[0]  # only where psf is defined
        psf_r68 = observation.psf.containment_radius(
            0.68, energy_true=etrue.edges, offset=0.0 * u.deg
        )

    psf_r68 = psf_r68[np.isfinite(psf_r68)]
    if psf_r68.size > 0:
        # remove outliers
        beyond_mad = np.median(psf_r68) - mad(psf_r68) * psf_r68.unit
        psf_r68[psf_r68 < beyond_mad] = np.nan
        binsz = np.nan_to_num(np.nanmin(psf_r68), nan=-np.inf)
        binsz = np.maximum(binsz, spatial_bin_size_min)
    else:
        binsz = spatial_bin_size_min
    return binsz


def _default_width(observation, spatial_width_max=12 * u.deg):
    # width estimated from the rad_max or the offset_max
    if isinstance(observation.psf, PSFMap):
        width = 2.0 * np.max(observation.psf.psf_map.geom.width)
    elif hasattr(observation.psf, "axes"):
        width = 2.0 * observation.psf.axes["offset"].edges[-1]
    else:
        width = spatial_width_max
    return np.minimum(width, spatial_width_max)


def create_empty_map_dataset_from_irfs(
    observation,
    dataset_name=None,
    energy_axis_true=None,
    energy_axis=None,
    energy_bin_per_decade_max=30,
    spatial_width=None,
    spatial_width_max=12 * u.deg,
    spatial_bin_size=None,
    spatial_bin_size_min=0.01 * u.deg,
    position=None,
    frame="icrs",
):
    """Create a MapDataset, if energy axes, spatial width or bin size are not given
    they are determined automatically from the IRFs,
    but the estimated value cannot exceed the given limits.

    Parameters
    ----------
    observation : `~gammapy.data.Observation`
        Observation containing the IRFs.
    dataset_name : str, optional
        Default is None. If None it is determined from the observation ID.
    energy_axis_true : `~gammapy.maps.MapAxis`, optional
        True energy axis. Default is None.
        If None it is determined from the observation IRFs.
    energy_axis : `~gammapy.maps.MapAxis`, optional
        Reconstructed energy axis. Default is None.
        If None it is determined from the observation IRFs.
    energy_bin_per_decade_max : int, optional
        Maximal number of bin per decade in energy for the reference dataset
    spatial_width : `~astropy.units.Quantity`, optional
        Spatial window size. Default is None.
        If None it is determined from the observation offset max or rad max.
    spatial_width_max : `~astropy.quantity.Quantity`, optional
        Maximal spatial width. Default is 12 degree.
    spatial_bin_size : `~astropy.units.Quantity`, optional
        Pixel size. Default is None.
        If None it is determined from the observation PSF R68.
    spatial_bin_size_min : `~astropy.quantity.Quantity`, optional
        Minimal spatial bin size. Default is 0.01 degree.
    position : `~astropy.coordinates.SkyCoord`, optional
        Center of the geometry. Default is the observation pointing at mid-observation time.
    frame: str, optional
        frame of the coordinate system. Default is "icrs".
    """

    if position is None:
        if hasattr(observation, "pointing"):
            if observation.pointing.mode is not PointingMode.POINTING:
                raise NotImplementedError(
                    "Only datas with fixed pointing in ICRS are supported"
                )
            position = observation.pointing.fixed_icrs

    if spatial_width is None:
        spatial_width = _default_width(observation, spatial_width_max)
    if spatial_bin_size is None:
        spatial_bin_size = _default_binsz(observation, spatial_bin_size_min)

    if energy_axis is None or energy_axis_true is None:
        energy_axis_, energy_axis_true_ = _default_energy_axis(
            observation, energy_bin_per_decade_max, position
        )

        if energy_axis is None:
            energy_axis = energy_axis_

        if energy_axis_true is None:
            energy_axis_true = energy_axis_true_

    if dataset_name is None:
        dataset_name = f"obs_{observation.obs_id}"

    geom = WcsGeom.create(
        skydir=position.transform_to(frame),
        width=spatial_width,
        binsz=spatial_bin_size.to_value(u.deg),
        frame=frame,
        axes=[energy_axis],
    )

    axes = dict(
        energy_axis_true=energy_axis_true,
    )
    if observation.edisp is not None:
        if isinstance(observation.edisp, EDispMap):
            axes["migra_axis"] = observation.edisp.edisp_map.geom.axes["migra"]
        elif hasattr(observation.edisp, "axes"):
            axes["migra_axis"] = observation.edisp.axes["migra"]

    dataset = MapDataset.create(
        geom,
        name=dataset_name,
        **axes,
    )
    return dataset


def create_map_dataset_from_observation(
    observation,
    models=None,
    dataset_name=None,
    energy_axis_true=None,
    energy_axis=None,
    energy_bin_per_decade_max=30,
    spatial_width=None,
    spatial_width_max=12 * u.deg,
    spatial_bin_size=None,
    spatial_bin_size_min=0.01 * u.deg,
    position=None,
    frame="icrs",
):
    """Create a MapDataset, if energy axes, spatial width or bin size are not given
    they are determined automatically from the observation IRFs,
    but the estimated value cannot exceed the given limits.

    Parameters
    ----------
    observation : `~gammapy.data.Observation`
        Observation to be simulated.
    models : `~gammapy.modeling.Models`, optional
        Models. Default is None.
    dataset_name : str, optional
        If `models` contains one or multiple `FoVBackgroundModel`
        it should match the `dataset_name` of the background model to use.
        Default is None. If None it is determined from the observation ID.
    energy_axis_true : `~gammapy.maps.MapAxis`, optional
        True energy axis. Default is None.
        If None it is determined from the observation IRFs.
    energy_axis : `~gammapy.maps.MapAxis`, optional
        Reconstructed energy axis. Default is None.
        If None it is determined from the observation IRFs.
    energy_bin_per_decade_max : int, optional
        Maximal number of bin per decade in energy for the reference dataset
    spatial_width : `~astropy.units.Quantity`, optional
        Spatial window size. Default is None.
         If None it is determined from the observation offset max or rad max.
    spatial_width_max : `~astropy.quantity.Quantity`, optional
        Maximal spatial width. Default is 12 degree.
    spatial_bin_size : `~astropy.units.Quantity`, optional
        Pixel size. Default is None.
        If None it is determined from the observation PSF R68.
    spatial_bin_size_min : `~astropy.quantity.Quantity`, optional
        Minimal spatial bin size. Default is 0.01 degree.
    position : `~astropy.coordinates.SkyCoord`, optional
        Center of the geometry. Default is the observation pointing.
    frame: str, optional
        frame of the coordinate system. Default is "icrs".
    """
    from gammapy.makers import MapDatasetMaker

    dataset = create_empty_map_dataset_from_irfs(
        observation,
        dataset_name=dataset_name,
        energy_axis_true=energy_axis_true,
        energy_axis=energy_axis,
        energy_bin_per_decade_max=energy_bin_per_decade_max,
        spatial_width=spatial_width,
        spatial_width_max=spatial_width_max,
        spatial_bin_size=spatial_bin_size,
        spatial_bin_size_min=spatial_bin_size_min,
        position=position,
        frame=frame,
    )

    if models is None:
        models = Models()
    if not np.any(
        [
            isinstance(m, FoVBackgroundModel) and m.datasets_names[0] == dataset.name
            for m in models
        ]
    ):
        models.append(FoVBackgroundModel(dataset_name=dataset.name))

    components = ["exposure"]
    if observation.edisp is not None:
        components.append("edisp")
    if observation.bkg is not None:
        components.append("background")
    if observation.psf is not None:
        components.append("psf")

    maker = MapDatasetMaker(selection=components)
    dataset = maker.run(dataset, observation)
    dataset.models = models
    return dataset


class MapDataset(Dataset):
    """Main map dataset for likelihood fitting.

    It bundles together binned counts, background, IRFs in the form of `~gammapy.maps.Map`.
    A safe mask and a fit mask can be added to exclude bins during the analysis.
    If models are assigned to it, it can compute predicted counts in each bin of the  counts `Map` and compute
    the associated statistic function, here the Cash statistic (see `~gammapy.stats.cash`).

    For more information see :ref:`datasets`.

    Parameters
    ----------
    models : `~gammapy.modeling.models.Models`
        Source sky models.
    counts : `~gammapy.maps.WcsNDMap` or `~gammapy.utils.fits.HDULocation`
        Counts cube.
    exposure : `~gammapy.maps.WcsNDMap` or `~gammapy.utils.fits.HDULocation`
        Exposure cube.
    background : `~gammapy.maps.WcsNDMap` or `~gammapy.utils.fits.HDULocation`
        Background cube.
    mask_fit : `~gammapy.maps.WcsNDMap` or `~gammapy.utils.fits.HDULocation`
        Mask to apply to the likelihood for fitting.
    psf : `~gammapy.irf.PSFMap` or `~gammapy.utils.fits.HDULocation`
        PSF kernel.
    edisp : `~gammapy.irf.EDispMap` or `~gammapy.utils.fits.HDULocation`
        Energy dispersion kernel
    mask_safe : `~gammapy.maps.WcsNDMap` or `~gammapy.utils.fits.HDULocation`
        Mask defining the safe data range.
    gti : `~gammapy.data.GTI`
        GTI of the observation or union of GTI if it is a stacked observation.
    meta_table : `~astropy.table.Table`
        Table listing information on observations used to create the dataset.
        One line per observation for stacked datasets.
    meta : `~gammapy.datasets.MapDatasetMetaData`
        Associated meta data container


    Notes
    -----

    If an `HDULocation` is passed the map is loaded lazily. This means the
    map data is only loaded in memory as the corresponding data attribute
    on the MapDataset is accessed. If it was accessed once it is cached for
    the next time.

    Examples
    --------
    >>> from gammapy.datasets import MapDataset
    >>> filename = "$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz"
    >>> dataset = MapDataset.read(filename, name="cta-dataset")
    >>> print(dataset)
    MapDataset
    ----------
    <BLANKLINE>
      Name                            : cta-dataset
    <BLANKLINE>
      Total counts                    : 104317
      Total background counts         : 91507.70
      Total excess counts             : 12809.30
    <BLANKLINE>
      Predicted counts                : 91507.69
      Predicted background counts     : 91507.70
      Predicted excess counts         : nan
    <BLANKLINE>
      Exposure min                    : 6.28e+07 m2 s
      Exposure max                    : 1.90e+10 m2 s
    <BLANKLINE>
      Number of total bins            : 768000
      Number of fit bins              : 691680
    <BLANKLINE>
      Fit statistic type              : cash
      Fit statistic value (-2 log(L)) : nan
    <BLANKLINE>
      Number of models                : 0
      Number of parameters            : 0
      Number of free parameters       : 0


    See Also
    --------
    MapDatasetOnOff, SpectrumDataset, FluxPointsDataset.
    """

    tag = "MapDataset"
    counts = LazyFitsData(cache=True)
    exposure = LazyFitsData(cache=True)
    edisp = LazyFitsData(cache=True)
    background = LazyFitsData(cache=True)
    psf = LazyFitsData(cache=True)
    mask_fit = LazyFitsData(cache=True)
    mask_safe = LazyFitsData(cache=True)

    _lazy_data_members = [
        "counts",
        "exposure",
        "edisp",
        "psf",
        "mask_fit",
        "mask_safe",
        "background",
    ]
    # TODO: shoule be part of the LazyFitsData no ?
    gti = None
    meta_table = None

    def __init__(
        self,
        models=None,
        counts=None,
        exposure=None,
        background=None,
        psf=None,
        edisp=None,
        mask_safe=None,
        mask_fit=None,
        gti=None,
        meta_table=None,
        name=None,
        meta=None,
        stat_type="cash",
    ):
        self._name = make_name(name)
        self._evaluators = {}

        self.counts = counts
        self.exposure = exposure
        self.background = background
        self._background_cached = None
        self._background_parameters_cached = None

        self.mask_fit = mask_fit

        if psf and not isinstance(psf, (PSFMap, HDULocation)):
            raise ValueError(
                f"'psf' must be a `PSFMap` or `HDULocation` object, got {type(psf)} instead."
            )
        self.psf = psf

        if edisp and not isinstance(edisp, (EDispMap, EDispKernelMap, HDULocation)):
            raise ValueError(
                "'edisp' must be a `EDispMap`, `EDispKernelMap` or `HDULocation` "
                f"object, got `{type(edisp)}` instead."
            )

        self.edisp = edisp
        self.mask_safe = mask_safe
        self.gti = gti
        self.models = models
        self.meta_table = meta_table
        self.meta = meta

        self.stat_type = stat_type

    @property
    def _psf_kernel(self):
        """Precompute PSFkernel if there is only one spatial bin in the PSFmap"""
        if self.psf and self.psf.has_single_spatial_bin:
            if self.psf.energy_name == "energy_true":
                map_ref = self.exposure
            else:
                map_ref = self.counts
            if map_ref and not map_ref.geom.is_region:
                return self.psf.get_psf_kernel(
                    position=map_ref.geom.center_skydir,
                    geom=map_ref.geom,
                    containment=meval.PSF_CONTAINMENT,
                    max_radius=meval.PSF_MAX_RADIUS,
                )

    @property
    def meta(self):
        return self._meta

    @meta.setter
    def meta(self, value):
        if value is None:
            self._meta = MapDatasetMetaData()
        else:
            self._meta = value

    # TODO: keep or remove?
    @property
    def background_model(self):
        if self.models and self.name in self.models.background_models.keys():
            return self.models[self.models.background_models[self.name]]

    def __str__(self):
        str_ = f"{self.__class__.__name__}\n"
        str_ += "-" * len(self.__class__.__name__) + "\n"
        str_ += "\n"
        str_ += "\t{:32}: {{name}} \n\n".format("Name")
        str_ += "\t{:32}: {{counts:.0f}} \n".format("Total counts")
        str_ += "\t{:32}: {{background:.2f}}\n".format("Total background counts")
        str_ += "\t{:32}: {{excess:.2f}}\n\n".format("Total excess counts")

        str_ += "\t{:32}: {{npred:.2f}}\n".format("Predicted counts")
        str_ += "\t{:32}: {{npred_background:.2f}}\n".format(
            "Predicted background counts"
        )
        str_ += "\t{:32}: {{npred_signal:.2f}}\n\n".format("Predicted excess counts")

        str_ += "\t{:32}: {{exposure_min:.2e}}\n".format("Exposure min")
        str_ += "\t{:32}: {{exposure_max:.2e}}\n\n".format("Exposure max")

        str_ += "\t{:32}: {{n_bins}} \n".format("Number of total bins")
        str_ += "\t{:32}: {{n_fit_bins}} \n\n".format("Number of fit bins")

        # likelihood section
        str_ += "\t{:32}: {{stat_type}}\n".format("Fit statistic type")
        str_ += "\t{:32}: {{stat_sum:.2f}}\n\n".format(
            "Fit statistic value (-2 log(L))"
        )

        info = self.info_dict()
        str_ = str_.format(**info)

        # model section
        n_models, n_pars, n_free_pars = 0, 0, 0
        if self.models is not None:
            n_models = len(self.models)
            n_pars = len(self.models.parameters)
            n_free_pars = len(self.models.parameters.free_parameters)

        str_ += "\t{:32}: {} \n".format("Number of models", n_models)
        str_ += "\t{:32}: {}\n".format("Number of parameters", n_pars)
        str_ += "\t{:32}: {}\n\n".format("Number of free parameters", n_free_pars)

        if self.models is not None:
            str_ += "\t" + "\n\t".join(str(self.models).split("\n")[2:])

        return str_.expandtabs(tabsize=2)

    @property
    def geoms(self):
        """Map geometries.

        Returns
        -------
        geoms : dict
            Dictionary of map geometries involved in the dataset.
        """
        geoms = {}

        geoms["geom"] = self._geom

        if self.exposure:
            geoms["geom_exposure"] = self.exposure.geom

        if self.psf:
            geoms["geom_psf"] = self.psf.psf_map.geom

        if self.edisp:
            geoms["geom_edisp"] = self.edisp.edisp_map.geom

        return geoms

    @property
    def models(self):
        """Models set on the dataset (`~gammapy.modeling.models.Models`)."""
        return self._models

    @property
    def excess(self):
        """Observed excess: counts-background."""
        return self.counts - self.background

    @models.setter
    def models(self, models):
        """Models setter."""
        self._evaluators = {}
        if models is not None:
            models = DatasetModels(models)
            models = models.select(datasets_names=self.name)
            if models:
                psf = self._psf_kernel
            for model in models:
                if not isinstance(model, FoVBackgroundModel):
                    evaluator = MapEvaluator(
                        model=model,
                        psf=psf,
                        evaluation_mode=EVALUATION_MODE,
                        gti=self.gti,
                        use_cache=USE_NPRED_CACHE,
                    )
                    self._evaluators[model.name] = evaluator

        self._models = models

    @property
    def evaluators(self):
        """Model evaluators."""
        return self._evaluators

    @property
    def _geom(self):
        """Main analysis geometry."""
        if self.counts is not None:
            return self.counts.geom
        elif self.background is not None:
            return self.background.geom
        elif self.mask_safe is not None:
            return self.mask_safe.geom
        elif self.mask_fit is not None:
            return self.mask_fit.geom
        else:
            raise ValueError(
                "Either 'counts', 'background', 'mask_fit'"
                " or 'mask_safe' must be defined."
            )

    @property
    def data_shape(self):
        """Shape of the counts or background data (tuple)."""
        return self._geom.data_shape

    def _energy_range(self, mask_map=None):
        """Compute the energy range maps with or without the fit mask."""
        geom = self._geom
        energy = geom.axes["energy"].edges
        e_i = geom.axes.index_data("energy")
        geom = geom.drop("energy")

        if mask_map is not None:
            mask = mask_map.data
            if mask.any():
                idx = mask.argmax(e_i)
                energy_min = energy.value[idx]
                mask_nan = ~mask.any(e_i)
                energy_min[mask_nan] = np.nan

                mask = np.flip(mask, e_i)
                idx = mask.argmax(e_i)
                energy_max = energy.value[::-1][idx]
                energy_max[mask_nan] = np.nan
            else:
                energy_min = np.full(geom.data_shape, np.nan)
                energy_max = energy_min.copy()
        else:
            data_shape = geom.data_shape
            energy_min = np.full(data_shape, energy.value[0])
            energy_max = np.full(data_shape, energy.value[-1])

        map_min = Map.from_geom(geom, data=energy_min, unit=energy.unit)
        map_max = Map.from_geom(geom, data=energy_max, unit=energy.unit)
        return map_min, map_max

    @property
    def energy_range(self):
        """Energy range maps defined by the mask_safe and mask_fit."""
        return self._energy_range(self.mask)

    @property
    def energy_range_safe(self):
        """Energy range maps defined by the mask_safe only."""
        return self._energy_range(self.mask_safe)

    @property
    def energy_range_fit(self):
        """Energy range maps defined by the mask_fit only."""
        return self._energy_range(self.mask_fit)

    @property
    def energy_range_total(self):
        """Largest energy range among all pixels, defined by mask_safe and mask_fit."""
        energy_min_map, energy_max_map = self.energy_range
        return np.nanmin(energy_min_map.quantity), np.nanmax(energy_max_map.quantity)

    def npred(self):
        """Total predicted source and background counts.

        Returns
        -------
        npred : `Map`
            Total predicted counts.
        """
        npred_total = self.npred_signal()

        if self.background:
            npred_total += self.npred_background()
        npred_total.data[npred_total.data < 0.0] = 0
        return npred_total

    def npred_background(self):
        """Predicted background counts.

        The predicted background counts depend on the parameters
        of the `FoVBackgroundModel` defined in the dataset.

        Returns
        -------
        npred_background : `Map`
            Predicted counts from the background.
        """
        background = self.background
        if self.background_model and background:
            if self._background_parameters_changed:
                values = self.background_model.evaluate_geom(geom=self.background.geom)
                if self._background_cached is None:
                    self._background_cached = background * values
                else:
                    self._background_cached.quantity = (
                        background.quantity * values.value
                    )
            return self._background_cached
        else:
            return background

        return background

    @property
    def _background_parameters_changed(self):
        values = self.background_model.parameters.value
        changed = ~np.all(self._background_parameters_cached == values)

        if changed:
            self._background_parameters_cached = values
        return changed

    def npred_signal(self, model_names=None, stack=True):
        """Model predicted signal counts.

        If a list of model name is passed, predicted counts from these components are returned.
        If stack is set to True, a map of the sum of all the predicted counts is returned.
        If stack is set to False, a map with an additional axis representing the models is returned.

        Parameters
        ----------
        model_names : list of str
            List of name of  SkyModel for which to compute the npred.
            If none, all the SkyModel predicted counts are computed.
        stack : bool
            Whether to stack the npred maps upon each other.

        Returns
        -------
        npred_sig : `gammapy.maps.Map`
            Map of the predicted signal counts.
        """
        npred_total = Map.from_geom(self._geom, dtype=float)

        evaluators = self.evaluators
        if model_names is not None:
            if isinstance(model_names, str):
                model_names = [model_names]
            evaluators = {name: self.evaluators[name] for name in model_names}

        npred_list = []
        labels = []
        for evaluator_name, evaluator in evaluators.items():
            if evaluator.needs_update:
                evaluator.update(
                    self.exposure,
                    self.psf,
                    self.edisp,
                    self._geom,
                    self.mask_image,
                )

            if evaluator.contributes:
                npred = evaluator.compute_npred()
                if stack:
                    npred_total.stack(npred)
                else:
                    npred_geom = Map.from_geom(self._geom, dtype=float)
                    npred_geom.stack(npred)
                    labels.append(evaluator_name)
                    npred_list.append(npred_geom)
                if not USE_NPRED_CACHE:
                    evaluator.reset_cache_properties()

        if npred_list != []:
            label_axis = LabelMapAxis(labels=labels, name="models")
            npred_total = Map.from_stack(npred_list, axis=label_axis)

        return npred_total

    @classmethod
    def from_geoms(
        cls,
        geom,
        geom_exposure=None,
        geom_psf=None,
        geom_edisp=None,
        reference_time="2000-01-01",
        name=None,
        **kwargs,
    ):
        """
        Create a MapDataset object with zero filled maps according to the specified geometries.

        Parameters
        ----------
        geom : `Geom`
            Geometry for the counts and background maps.
        geom_exposure : `Geom`
            Geometry for the exposure map. Default is None.
        geom_psf : `Geom`
            Geometry for the PSF map. Default is None.
        geom_edisp : `Geom`
            Geometry for the energy dispersion kernel map.
            If geom_edisp has a migra axis, this will create an EDispMap instead. Default is None.
        reference_time : `~astropy.time.Time`
            The reference time to use in GTI definition. Default is "2000-01-01".
        name : str
            Name of the returned dataset. Default is None.
        kwargs : dict, optional
            Keyword arguments to be passed.


        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            A dataset containing zero filled maps.
        """
        name = make_name(name)
        kwargs = kwargs.copy()
        kwargs["name"] = name
        kwargs["counts"] = Map.from_geom(geom, unit="")
        kwargs["background"] = Map.from_geom(geom, unit="")

        if geom_exposure:
            kwargs["exposure"] = Map.from_geom(geom_exposure, unit="m2 s")

        if geom_edisp:
            if "energy" in geom_edisp.axes.names:
                kwargs["edisp"] = EDispKernelMap.from_geom(geom_edisp)
            else:
                kwargs["edisp"] = EDispMap.from_geom(geom_edisp)

        if geom_psf:
            if "energy_true" in geom_psf.axes.names:
                kwargs["psf"] = PSFMap.from_geom(geom_psf)
            elif "energy" in geom_psf.axes.names:
                kwargs["psf"] = RecoPSFMap.from_geom(geom_psf)

        kwargs.setdefault(
            "gti", GTI.create([] * u.s, [] * u.s, reference_time=reference_time)
        )
        kwargs["mask_safe"] = Map.from_geom(geom, unit="", dtype=bool)
        return cls(**kwargs)

    @classmethod
    def create(
        cls,
        geom,
        energy_axis_true=None,
        migra_axis=None,
        rad_axis=None,
        binsz_irf=BINSZ_IRF_DEFAULT,
        reference_time="2000-01-01",
        name=None,
        meta_table=None,
        reco_psf=False,
        **kwargs,
    ):
        """Create a MapDataset object with zero filled maps.

        Parameters
        ----------
        geom : `~gammapy.maps.WcsGeom`
            Reference target geometry in reco energy, used for counts and background maps.
        energy_axis_true : `~gammapy.maps.MapAxis`, optional
            True energy axis used for IRF maps. Default is None.
        migra_axis : `~gammapy.maps.MapAxis`, optional
            If set, this provides the migration axis for the energy dispersion map.
            If not set, an EDispKernelMap is produced instead. Default is None.
        rad_axis : `~gammapy.maps.MapAxis`, optional
            Rad axis for the PSF map. Default is None.
        binsz_irf : float
            IRF Map pixel size in degrees. Default is BINSZ_IRF_DEFAULT.
        reference_time : `~astropy.time.Time`
            The reference time to use in GTI definition. Default is "2000-01-01".
        name : str, optional
            Name of the returned dataset. Default is None.
        meta_table : `~astropy.table.Table`, optional
            Table listing information on observations used to create the dataset.
            One line per observation for stacked datasets. Default is None.
        reco_psf : bool
            Use reconstructed energy for the PSF geometry. Default is False.

        Returns
        -------
        empty_maps : `MapDataset`
            A MapDataset containing zero filled maps.

        Examples
        --------
        >>> from gammapy.datasets import MapDataset
        >>> from gammapy.maps import WcsGeom, MapAxis

        >>> energy_axis = MapAxis.from_energy_bounds(1.0, 10.0, 4, unit="TeV")
        >>> energy_axis_true = MapAxis.from_energy_bounds(
        ...            0.5, 20, 10, unit="TeV", name="energy_true"
        ...        )
        >>> geom = WcsGeom.create(
        ...            skydir=(83.633, 22.014),
        ...            binsz=0.02, width=(2, 2),
        ...            frame="icrs",
        ...            proj="CAR",
        ...            axes=[energy_axis]
        ...        )
        >>> empty = MapDataset.create(geom=geom, energy_axis_true=energy_axis_true, name="empty")
        """

        geoms = create_map_dataset_geoms(
            geom=geom,
            energy_axis_true=energy_axis_true,
            rad_axis=rad_axis,
            migra_axis=migra_axis,
            binsz_irf=binsz_irf,
            reco_psf=reco_psf,
        )

        kwargs.update(geoms)
        return cls.from_geoms(
            reference_time=reference_time, name=name, meta_table=meta_table, **kwargs
        )

    @property
    def mask_safe_image(self):
        """Reduced safe mask."""
        if self.mask_safe is None:
            return None
        return self.mask_safe.reduce_over_axes(func=np.logical_or)

    @property
    def mask_fit_image(self):
        """Reduced fit mask."""
        if self.mask_fit is None:
            return None
        return self.mask_fit.reduce_over_axes(func=np.logical_or)

    @property
    def mask_image(self):
        """Reduced mask."""
        if self.mask is None:
            mask = Map.from_geom(self._geom.to_image(), dtype=bool)
            mask.data |= True
            return mask

        return self.mask.reduce_over_axes(func=np.logical_or)

    @property
    def mask_safe_psf(self):
        """Safe mask for PSF maps."""
        if self.mask_safe is None or self.psf is None:
            return None

        geom = self.psf.psf_map.geom.squash("energy_true").squash("rad")
        mask_safe_psf = self.mask_safe_image.interp_to_geom(geom.to_image())
        return mask_safe_psf.to_cube(geom.axes)

    @property
    def mask_safe_edisp(self):
        """Safe mask for edisp maps."""
        if self.mask_safe is None or self.edisp is None:
            return None

        if self.mask_safe.geom.is_region:
            return self.mask_safe

        geom = self.edisp.edisp_map.geom.squash("energy_true")

        if "migra" in geom.axes.names:
            geom = geom.squash("migra")
            mask_safe_edisp = self.mask_safe_image.interp_to_geom(
                geom.to_image(), fill_value=None
            )
            return mask_safe_edisp.to_cube(geom.axes)

        # allow extrapolation only along spatial dimension
        # to support case where mask_safe geom and irfs geom are different
        geom_same_axes = geom.to_image().to_cube(self.mask_safe.geom.axes)
        mask_safe_edisp = self.mask_safe.interp_to_geom(geom_same_axes, fill_value=None)
        mask_safe_edisp = mask_safe_edisp.interp_to_geom(geom)
        return mask_safe_edisp

    def to_masked(self, name=None, nan_to_num=True):
        """Return masked dataset.

        Parameters
        ----------
        name : str, optional
            Name of the masked dataset. Default is None.
        nan_to_num : bool
            Non-finite values are replaced by zero if True. Default is True.

        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            Masked dataset.
        """
        dataset = self.__class__.from_geoms(**self.geoms, name=name)
        dataset.stack(self, nan_to_num=nan_to_num)
        return dataset

    def stack(self, other, nan_to_num=True):
        r"""Stack another dataset in place. The original dataset is modified.

        Safe mask is applied to the other dataset to compute the stacked counts data.
        Counts outside the safe mask are lost.

        Note that the masking is not applied to the current dataset. If masking needs
        to be applied to it, use `~gammapy.MapDataset.to_masked()` first.

        The stacking of 2 datasets is implemented as follows. Here, :math:`k`
        denotes a bin in reconstructed energy and :math:`j = {1,2}` is the dataset number.

        The ``mask_safe`` of each dataset is defined as:

        .. math::

            \epsilon_{jk} =\left\{\begin{array}{cl} 1, &
            \mbox{if bin k is inside the thresholds}\\ 0, &
            \mbox{otherwise} \end{array}\right.

        Then the total ``counts`` and model background ``bkg`` are computed according to:

        .. math::

            \overline{\mathrm{n_{on}}}_k =  \mathrm{n_{on}}_{1k} \cdot \epsilon_{1k} +
             \mathrm{n_{on}}_{2k} \cdot \epsilon_{2k}.

            \overline{bkg}_k = bkg_{1k} \cdot \epsilon_{1k} +
             bkg_{2k} \cdot \epsilon_{2k}.

        The stacked ``safe_mask`` is then:

        .. math::

            \overline{\epsilon_k} = \epsilon_{1k} OR \epsilon_{2k}.

        For details, see :ref:`stack`.

        Parameters
        ----------
        other : `~gammapy.datasets.MapDataset` or `~gammapy.datasets.MapDatasetOnOff`
            Map dataset to be stacked with this one. If other is an on-off
            dataset alpha * counts_off is used as a background model.
        nan_to_num : bool
            Non-finite values are replaced by zero if True. Default is True.

        """
        if self.counts and other.counts:
            self.counts.stack(
                other.counts, weights=other.mask_safe, nan_to_num=nan_to_num
            )

        if self.exposure and other.exposure:
            self.exposure.stack(
                other.exposure, weights=other.mask_safe_image, nan_to_num=nan_to_num
            )
            # TODO: check whether this can be improved e.g. handling this in GTI

            if "livetime" in other.exposure.meta and np.any(other.mask_safe_image):
                if "livetime" in self.exposure.meta:
                    self.exposure.meta["livetime"] += other.exposure.meta["livetime"]
                else:
                    self.exposure.meta["livetime"] = other.exposure.meta[
                        "livetime"
                    ].copy()

        if self.stat_type == "cash":
            if self.background and other.background:
                background = self.npred_background()
                background.stack(
                    other.npred_background(),
                    weights=other.mask_safe,
                    nan_to_num=nan_to_num,
                )
                self.background = background

        if self.psf and other.psf:
            self.psf.stack(other.psf, weights=other.mask_safe_psf)

        if self.edisp and other.edisp:
            self.edisp.stack(other.edisp, weights=other.mask_safe_edisp)

        if self.mask_safe and other.mask_safe:
            self.mask_safe.stack(other.mask_safe)

        if self.mask_fit and other.mask_fit:
            self.mask_fit.stack(other.mask_fit)
        elif other.mask_fit:
            self.mask_fit = other.mask_fit.copy()

        if self.gti and other.gti:
            self.gti.stack(other.gti)
            self.gti = self.gti.union()

        if self.meta_table and other.meta_table:
            self.meta_table = hstack_columns(self.meta_table, other.meta_table)
        elif other.meta_table:
            self.meta_table = other.meta_table.copy()

        if self.meta and other.meta:
            self.meta.stack(other.meta)

    def residuals(self, method="diff", **kwargs):
        """Compute residuals map.

        Parameters
        ----------
        method : {"diff", "diff/model", "diff/sqrt(model)"}
            Method used to compute the residuals. Available options are:

            - "diff" (default): data - model.
            - "diff/model": (data - model) / model.
            - "diff/sqrt(model)": (data - model) / sqrt(model).

            Default is "diff".

        **kwargs : dict, optional
            Keyword arguments forwarded to `Map.smooth()`.

        Returns
        -------
        residuals : `gammapy.maps.Map`
            Residual map.
        """
        npred, counts = self.npred(), self.counts.copy()

        if self.mask:
            npred = npred * self.mask
            counts = counts * self.mask

        if kwargs:
            kwargs.setdefault("mode", "constant")
            kwargs.setdefault("width", "0.1 deg")
            kwargs.setdefault("kernel", "gauss")
            with np.errstate(invalid="ignore", divide="ignore"):
                npred = npred.smooth(**kwargs)
                counts = counts.smooth(**kwargs)
                if self.mask:
                    mask = self.mask.smooth(**kwargs)
                    npred /= mask
                    counts /= mask

        residuals = self._compute_residuals(counts, npred, method=method)

        if self.mask:
            residuals.data[~self.mask.data] = np.nan

        return residuals

    def plot_residuals_spatial(
        self,
        ax=None,
        method="diff",
        smooth_kernel="gauss",
        smooth_radius="0.1 deg",
        **kwargs,
    ):
        """Plot spatial residuals.

        The normalization used for the residuals computation can be controlled
        using the method parameter.

        Parameters
        ----------
        ax : `~astropy.visualization.wcsaxes.WCSAxes`, optional
            Axes to plot on. Default is None.
        method : {"diff", "diff/model", "diff/sqrt(model)"}
            Normalization used to compute the residuals, see `MapDataset.residuals`. Default is "diff".
        smooth_kernel : {"gauss", "box"}
            Kernel shape. Default is "gauss".
        smooth_radius: `~astropy.units.Quantity`, str or float
            Smoothing width given as quantity or float. If a float is given, it
            is interpreted as smoothing width in pixels. Default is "0.1 deg".
        **kwargs : dict, optional
            Keyword arguments passed to `~matplotlib.axes.Axes.imshow`.

        Returns
        -------
        ax : `~astropy.visualization.wcsaxes.WCSAxes`
            WCSAxes object.

        Examples
        --------
        >>> from gammapy.datasets import MapDataset
        >>> dataset = MapDataset.read("$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz")
        >>> kwargs = {"cmap": "RdBu_r", "vmin":-5, "vmax":5, "add_cbar": True}
        >>> dataset.plot_residuals_spatial(method="diff/sqrt(model)", **kwargs) # doctest: +SKIP
        """
        counts, npred = self.counts.copy(), self.npred()

        if counts.geom.is_region:
            raise ValueError("Cannot plot spatial residuals for RegionNDMap")

        if self.mask is not None:
            counts *= self.mask
            npred *= self.mask

        counts_spatial = counts.sum_over_axes().smooth(
            width=smooth_radius, kernel=smooth_kernel
        )
        npred_spatial = npred.sum_over_axes().smooth(
            width=smooth_radius, kernel=smooth_kernel
        )
        residuals = self._compute_residuals(counts_spatial, npred_spatial, method)

        if self.mask is not None:
            mask = self.mask.reduce_over_axes(func=np.logical_or, keepdims=True)
            residuals.data[~mask.data] = np.nan

        kwargs.setdefault("add_cbar", True)
        kwargs.setdefault("cmap", "coolwarm")
        kwargs.setdefault("vmin", -5)
        kwargs.setdefault("vmax", 5)
        ax = residuals.plot(ax, **kwargs)
        return ax

    def plot_residuals_spectral(
        self,
        ax=None,
        method="diff",
        region=None,
        kwargs_fit=None,
        kwargs_safe=None,
        **kwargs,
    ):
        """Plot spectral residuals.

        The residuals are extracted from the provided region, and the normalization
        used for its computation can be controlled using the method parameter.

        Both the mask fit and mask safe are taken into account.

        The error bars are computed using the uncertainty on the excess with a symmetric assumption.

        Parameters
        ----------
        ax : `~matplotlib.axes.Axes`, optional
            Axes to plot on. Default is None.
        method : {"diff", "diff/sqrt(model)"}, optional
            Normalization used to compute the residuals, see `SpectrumDataset.residuals`.
            Default is "diff".
        region : `~regions.SkyRegion`, optional
            Target sky region. If None, the full dataset region
            (i.e., `~gammapy.maps.WcsGeom.footprint_rectangle_sky_region`) is used as the default.
            Default is None.
        kwargs_fit : dict, optional
            Keyword arguments passed to `~RegionNDMap.plot_mask()` for mask fit.
            Default is None.
        kwargs_safe : dict, optional
            Keyword arguments passed to `~RegionNDMap.plot_mask()` for mask safe.
            Default is None.
        **kwargs : dict, optional
            Keyword arguments passed to `~matplotlib.axes.Axes.errorbar`.

        Returns
        -------
        ax : `~matplotlib.axes.Axes`
            Axes object.

        Examples
        --------
        >>> from gammapy.datasets import MapDataset
        >>> dataset = MapDataset.read("$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz")
        >>> kwargs = {"markerfacecolor": "blue", "markersize":8, "marker":'s'}
        >>> dataset.plot_residuals_spectral(method="diff/sqrt(model)", **kwargs) # doctest: +SKIP

        """
        counts, npred = self.counts.copy(), self.npred()
        if self.mask is not None:
            counts *= self.mask
            npred *= self.mask
        counts_spec = counts.get_spectrum(region)
        npred_spec = npred.get_spectrum(region)
        residuals = self._compute_residuals(counts_spec, npred_spec, method)

        if self.stat_type == "wstat":
            counts_off = (self.counts_off).get_spectrum(region)

            with np.errstate(invalid="ignore"):
                alpha = self.background.get_spectrum(region) / counts_off

            mu_sig = self.npred_signal().get_spectrum(region)
            stat = WStatCountsStatistic(
                n_on=counts_spec,
                n_off=counts_off,
                alpha=alpha,
                mu_sig=mu_sig,
            )
        elif self.stat_type == "cash":
            stat = CashCountsStatistic(counts_spec.data, npred_spec.data)
        excess_error = stat.error

        if method == "diff":
            yerr = excess_error
        elif method == "diff/sqrt(model)":
            yerr = excess_error / np.sqrt(npred_spec.data)
        else:
            raise ValueError(
                'Invalid method, choose between "diff" and "diff/sqrt(model)"'
            )

        kwargs.setdefault("color", kwargs.pop("c", "black"))
        ax = residuals.plot(ax, yerr=yerr, **kwargs)
        ax.axhline(0, color=kwargs["color"], lw=0.5)

        label = self._residuals_labels[method]
        ax.set_ylabel(f"Residuals ({label})")
        ax.set_yscale("linear")
        ymin = 1.05 * np.nanmin(residuals.data - yerr)
        ymax = 1.05 * np.nanmax(residuals.data + yerr)
        ax.set_ylim(ymin, ymax)

        kwargs_fit = kwargs_fit or {}
        kwargs_safe = kwargs_safe or {}

        kwargs_fit.setdefault("label", "Mask fit")
        kwargs_fit.setdefault("color", "tab:green")
        kwargs_safe.setdefault("label", "Mask safe")
        kwargs_safe.setdefault("color", "black")

        if self.mask_fit:
            self.mask_fit.to_region_nd_map().plot_mask(ax=ax, **kwargs_fit)

        if self.mask_safe:
            self.mask_safe.to_region_nd_map().plot_mask(ax=ax, **kwargs_safe)
        ax.legend()
        return ax

    def plot_residuals(
        self,
        ax_spatial=None,
        ax_spectral=None,
        kwargs_spatial=None,
        kwargs_spectral=None,
    ):
        """Plot spatial and spectral residuals in two panels.

        Calls `~MapDataset.plot_residuals_spatial` and `~MapDataset.plot_residuals_spectral`.
        The spectral residuals are extracted from the provided region, and the
        normalization used for its computation can be controlled using the method
        parameter. The region outline is overlaid on the residuals map. If no region is passed,
        the residuals are computed for the entire map.

        Parameters
        ----------
        ax_spatial : `~astropy.visualization.wcsaxes.WCSAxes`, optional
            Axes to plot spatial residuals on. Default is None.
        ax_spectral : `~matplotlib.axes.Axes`, optional
            Axes to plot spectral residuals on. Default is None.
        kwargs_spatial : dict, optional
            Keyword arguments passed to `~MapDataset.plot_residuals_spatial`. Default is None.
        kwargs_spectral : dict, optional
            Keyword arguments passed to `~MapDataset.plot_residuals_spectral`.
            The region should be passed as a dictionary key. Default is None.

        Returns
        -------
        ax_spatial, ax_spectral : `~astropy.visualization.wcsaxes.WCSAxes`, `~matplotlib.axes.Axes`
            Spatial and spectral residuals plots.

        Examples
        --------
        >>> from regions import CircleSkyRegion
        >>> from astropy.coordinates import SkyCoord
        >>> import astropy.units as u
        >>> from gammapy.datasets import MapDataset
        >>> dataset = MapDataset.read("$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz")
        >>> reg = CircleSkyRegion(SkyCoord(0,0, unit="deg", frame="galactic"), radius=1.0 * u.deg)
        >>> kwargs_spatial = {"cmap": "RdBu_r", "vmin":-5, "vmax":5, "add_cbar": True}
        >>> kwargs_spectral = {"region":reg, "markerfacecolor": "blue", "markersize": 8, "marker": "s"}
        >>> dataset.plot_residuals(kwargs_spatial=kwargs_spatial, kwargs_spectral=kwargs_spectral) # doctest: +SKIP
        """
        ax_spatial, ax_spectral = get_axes(
            ax_spatial,
            ax_spectral,
            12,
            4,
            [1, 2, 1],
            [1, 2, 2],
            {"projection": self._geom.to_image().wcs},
        )
        kwargs_spatial = kwargs_spatial or {}
        kwargs_spectral = kwargs_spectral or {}

        self.plot_residuals_spatial(ax_spatial, **kwargs_spatial)
        self.plot_residuals_spectral(ax_spectral, **kwargs_spectral)

        # Overlay spectral extraction region on the spatial residuals
        region = kwargs_spectral.get("region")
        if region is not None:
            pix_region = region.to_pixel(self._geom.to_image().wcs)
            pix_region.plot(ax=ax_spatial)

        return ax_spatial, ax_spectral

    def _to_asimov_dataset(self):
        """Create Asimov dataset from the current models."""

        npred = self.npred()
        data = np.nan_to_num(npred.data, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        npred.data = data.astype("float")

        asimov_dataset = self.__class__(
            models=self.models,
            counts=npred,
            exposure=self.exposure,
            background=self.background,
            psf=self.psf,
            edisp=self.edisp,
            mask_safe=self.mask_safe,
            mask_fit=self.mask_fit,
            gti=self.gti,
            name=self.name,
            meta=self.meta,
        )
        asimov_dataset._evaluators = self._evaluators
        return asimov_dataset

    def fake(self, random_state="random-seed"):
        """Simulate fake counts for the current model and reduced IRFs.

        This method overwrites the counts defined on the dataset object.

        Parameters
        ----------
        random_state : {int, 'random-seed', 'global-rng', `~numpy.random.RandomState`}
                Defines random number generator initialisation.
                Passed to `~gammapy.utils.random.get_random_state`. Default is "random-seed".
        """
        random_state = get_random_state(random_state)
        npred = self.npred()
        data = np.nan_to_num(npred.data, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        npred.data = random_state.poisson(data)
        npred.data = npred.data.astype("float")
        self.counts = npred

    def to_hdulist(self):
        """Convert map dataset to list of HDUs.

        Returns
        -------
        hdulist : `~astropy.io.fits.HDUList`
            Map dataset list of HDUs.
        """
        # TODO: what todo about the model and background model parameters?
        exclude_primary = slice(1, None)

        hdu_primary = fits.PrimaryHDU()

        header = hdu_primary.header
        header["NAME"] = self.name
        header.update(self.meta.to_header())
        creation = self.meta.creation
        creation.update_time()

        hdulist = fits.HDUList([hdu_primary])
        if self.counts is not None:
            hdulist += self.counts.to_hdulist(hdu="counts")[exclude_primary]

        if self.exposure is not None:
            hdulist += self.exposure.to_hdulist(hdu="exposure")[exclude_primary]

        if self.background is not None:
            hdulist += self.background.to_hdulist(hdu="background")[exclude_primary]

        if self.edisp is not None:
            hdulist += self.edisp.to_hdulist()[exclude_primary]

        if self.psf is not None:
            hdulist += self.psf.to_hdulist()[exclude_primary]

        if self.mask_safe is not None:
            hdulist += self.mask_safe.to_hdulist(hdu="mask_safe")[exclude_primary]

        if self.mask_fit is not None:
            hdulist += self.mask_fit.to_hdulist(hdu="mask_fit")[exclude_primary]

        if self.gti is not None:
            hdulist.append(self.gti.to_table_hdu())

        if self.meta_table is not None:
            hdulist.append(fits.BinTableHDU(self.meta_table, name="META_TABLE"))

        for hdu in hdulist:
            hdu.header.update(creation.to_header())

        return hdulist

    @classmethod
    def from_hdulist(cls, hdulist, name=None, lazy=False, format="gadf"):
        """Create map dataset from list of HDUs.

        Parameters
        ----------
        hdulist : `~astropy.io.fits.HDUList`
            List of HDUs.
        name : str, optional
            Name of the new dataset. Default is None.
        lazy : bool
            Whether to lazy load data into memory. Default is False.
        format : {"gadf"}
            Format the hdulist is given in. Default is "gadf".

        Returns
        -------
        dataset : `MapDataset`
            Map dataset.
        """
        name = make_name(name)
        kwargs = {"name": name}
        kwargs["meta"] = MapDatasetMetaData.from_header(hdulist["PRIMARY"].header)

        if "COUNTS" in hdulist:
            kwargs["counts"] = Map.from_hdulist(hdulist, hdu="counts", format=format)

        if "EXPOSURE" in hdulist:
            exposure = Map.from_hdulist(hdulist, hdu="exposure", format=format)
            if exposure.geom.axes[0].name == "energy":
                exposure.geom.axes[0].name = "energy_true"
            kwargs["exposure"] = exposure

        if "BACKGROUND" in hdulist:
            kwargs["background"] = Map.from_hdulist(
                hdulist, hdu="background", format=format
            )

        if "EDISP" in hdulist:
            kwargs["edisp"] = EDispMap.from_hdulist(
                hdulist, hdu="edisp", exposure_hdu="edisp_exposure", format=format
            )

        if "PSF" in hdulist:
            kwargs["psf"] = PSFMap.from_hdulist(
                hdulist, hdu="psf", exposure_hdu="psf_exposure", format=format
            )

        if "MASK_SAFE" in hdulist:
            mask_safe = Map.from_hdulist(hdulist, hdu="mask_safe", format=format)
            mask_safe.data = mask_safe.data.astype(bool)
            kwargs["mask_safe"] = mask_safe

        if "MASK_FIT" in hdulist:
            mask_fit = Map.from_hdulist(hdulist, hdu="mask_fit", format=format)
            mask_fit.data = mask_fit.data.astype(bool)
            kwargs["mask_fit"] = mask_fit

        if "GTI" in hdulist:
            gti = GTI.from_table_hdu(hdulist["GTI"])
            kwargs["gti"] = gti

        if "META_TABLE" in hdulist:
            meta_table = Table.read(hdulist, hdu="META_TABLE")
            kwargs["meta_table"] = meta_table

        return cls(**kwargs)

    def write(self, filename, overwrite=False, checksum=False):
        """Write Dataset to file.

        A MapDataset is serialised using the GADF format with a WCS geometry.
        A SpectrumDataset uses the same format, with a RegionGeom.

        Parameters
        ----------
        filename : str
            Filename to write to.
        overwrite : bool, optional
            Overwrite existing file. Default is False.
        checksum : bool
            When True adds both DATASUM and CHECKSUM cards to the headers written to the file.
            Default is False.
        """
        self.to_hdulist().writeto(
            str(make_path(filename)), overwrite=overwrite, checksum=checksum
        )

    @classmethod
    def _read_lazy(cls, name, filename, cache, format=format):
        name = make_name(name)
        kwargs = {"name": name}
        try:
            kwargs["gti"] = GTI.read(filename)
        except KeyError:
            pass

        path = make_path(filename)
        for hdu_name in ["counts", "exposure", "mask_fit", "mask_safe", "background"]:
            kwargs[hdu_name] = HDULocation(
                hdu_class="map",
                file_dir=path.parent,
                file_name=path.name,
                hdu_name=hdu_name.upper(),
                cache=cache,
                format=format,
            )

        kwargs["edisp"] = HDULocation(
            hdu_class="edisp_map",
            file_dir=path.parent,
            file_name=path.name,
            hdu_name="EDISP",
            cache=cache,
            format=format,
        )

        kwargs["psf"] = HDULocation(
            hdu_class="psf_map",
            file_dir=path.parent,
            file_name=path.name,
            hdu_name="PSF",
            cache=cache,
            format=format,
        )

        return cls(**kwargs)

    @classmethod
    def read(
        cls, filename, name=None, lazy=False, cache=True, format="gadf", checksum=False
    ):
        """Read a dataset from file.

        Parameters
        ----------
        filename : str
            Filename to read from.
        name : str, optional
            Name of the new dataset. Default is None.
        lazy : bool
            Whether to lazy load data into memory. Default is False.
        cache : bool
            Whether to cache the data after loading. Default is True.
        format : {"gadf"}
            Format of the dataset file. Default is "gadf".
        checksum : bool
            If True checks both DATASUM and CHECKSUM cards in the file headers. Default is False.

        Returns
        -------
        dataset : `MapDataset`
            Map dataset.
        """

        if name is None:
            header = fits.getheader(str(make_path(filename)))
            name = header.get("NAME", name)
        ds_name = make_name(name)

        if lazy:
            return cls._read_lazy(
                name=ds_name, filename=filename, cache=cache, format=format
            )
        else:
            with fits.open(
                str(make_path(filename)), memmap=False, checksum=checksum
            ) as hdulist:
                return cls.from_hdulist(hdulist, name=ds_name, format=format)

    @classmethod
    def from_dict(cls, data, lazy=False, cache=True):
        """Create from dicts and models list generated from YAML serialization."""
        filename = make_path(data["filename"])
        dataset = cls.read(filename, name=data["name"], lazy=lazy, cache=cache)
        return dataset

    @property
    def _counts_statistic(self):
        """Counts statistics of the dataset."""
        return CashCountsStatistic(self.counts, self.npred_background())

    def info_dict(self, in_safe_data_range=True):
        """Info dict with summary statistics, summed over energy.

        Parameters
        ----------
        in_safe_data_range : bool
            Whether to sum only in the safe energy range. Default is True.

        Returns
        -------
        info_dict : dict
            Dictionary with summary info.
        """
        info = {}
        info["name"] = self.name

        if self.mask_safe and in_safe_data_range:
            mask = self.mask_safe.data.astype(bool)
        else:
            mask = slice(None)

        counts = 0
        background, excess, sqrt_ts = np.nan, np.nan, np.nan

        if self.counts:
            counts = self.counts.data[mask].sum()

            if self.background:
                summed_stat = self._counts_statistic[mask].sum()
                background = self.background.data[mask].sum()
                excess = summed_stat.n_sig
                sqrt_ts = summed_stat.sqrt_ts

        info["counts"] = int(counts)
        info["excess"] = float(excess)
        info["sqrt_ts"] = sqrt_ts
        info["background"] = float(background)

        npred = np.nan
        if self.models or not np.isnan(background):
            npred = self.npred().data[mask].sum()

        info["npred"] = float(npred)

        npred_background = np.nan
        if self.background:
            npred_background = self.npred_background().data[mask].sum()

        info["npred_background"] = float(npred_background)

        npred_signal = np.nan
        if self.models and (
            len(self.models) > 1 or not isinstance(self.models[0], FoVBackgroundModel)
        ):
            npred_signal = self.npred_signal().data[mask].sum()

        info["npred_signal"] = float(npred_signal)

        exposure_min = np.nan * u.Unit("cm s")
        exposure_max = np.nan * u.Unit("cm s")
        livetime = np.nan * u.s

        if self.exposure is not None:
            mask_exposure = self.exposure.data > 0

            if self.mask_safe is not None:
                mask_spatial = self.mask_safe.reduce_over_axes(func=np.logical_or).data
                mask_exposure = mask_exposure & mask_spatial[np.newaxis, :, :]

            if not mask_exposure.any():
                mask_exposure = slice(None)

            exposure_min = np.min(self.exposure.quantity[mask_exposure])
            exposure_max = np.max(self.exposure.quantity[mask_exposure])
            livetime = self.exposure.meta.get("livetime", np.nan * u.s).copy()

        info["exposure_min"] = exposure_min.item()
        info["exposure_max"] = exposure_max.item()
        info["livetime"] = livetime

        ontime = u.Quantity(np.nan, "s")
        if self.gti:
            ontime = self.gti.time_sum

        info["ontime"] = ontime

        info["counts_rate"] = info["counts"] / info["livetime"]
        info["background_rate"] = info["background"] / info["livetime"]
        info["excess_rate"] = info["excess"] / info["livetime"]

        # data section
        n_bins = 0
        if self.counts is not None:
            n_bins = self.counts.data.size
        info["n_bins"] = int(n_bins)

        n_fit_bins = 0
        if self.mask is not None:
            n_fit_bins = np.sum(self.mask.data)

        info["n_fit_bins"] = int(n_fit_bins)
        info["stat_type"] = self.stat_type

        stat_sum = np.nan
        if self.counts is not None and self.models is not None:
            stat_sum = self.stat_sum()

        info["stat_sum"] = float(stat_sum)

        return info

    def to_spectrum_dataset(self, on_region, containment_correction=False, name=None):
        """Return a ~gammapy.datasets.SpectrumDataset from on_region.

        Counts and background are summed in the on_region. Exposure is taken
        from the average exposure.

        The energy dispersion kernel is obtained at the on_region center.
        Only regions with centers are supported.

        The model is not exported to the ~gammapy.datasets.SpectrumDataset.
        It must be set after the dataset extraction.

        Parameters
        ----------
        on_region : `~regions.SkyRegion`
            The input ON region on which to extract the spectrum.
        containment_correction : bool
            Apply containment correction for point sources and circular on regions. Default is False.
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `~gammapy.datasets.SpectrumDataset`
            The resulting reduced dataset.
        """
        from .spectrum import SpectrumDataset

        dataset = self.to_region_map_dataset(region=on_region, name=name)

        if containment_correction:
            if not isinstance(on_region, CircleSkyRegion):
                raise TypeError(
                    "Containment correction is only supported for" " `CircleSkyRegion`."
                )
            elif self.psf is None or isinstance(self.psf, PSFKernel):
                raise ValueError("No PSFMap set. Containment correction impossible")
            else:
                geom = dataset.exposure.geom
                energy_true = geom.axes["energy_true"].center
                containment = self.psf.containment(
                    position=on_region.center,
                    energy_true=energy_true,
                    rad=on_region.radius,
                )
                dataset.exposure.quantity *= containment.reshape(geom.data_shape)

        kwargs = {"name": name}

        for key in [
            "counts",
            "edisp",
            "mask_safe",
            "mask_fit",
            "exposure",
            "gti",
            "meta_table",
        ]:
            kwargs[key] = getattr(dataset, key)

        if self.stat_type == "cash":
            kwargs["background"] = dataset.background

        return SpectrumDataset(**kwargs)

    def to_region_map_dataset(self, region, name=None):
        """Integrate the map dataset in a given region.

        Counts and background of the dataset are integrated in the given region,
        taking the safe mask into account. The exposure is averaged in the
        region. The PSF and energy
        dispersion kernel are taken at the center of the region.

        Parameters
        ----------
        region : `~regions.SkyRegion`
            Region from which to extract the spectrum.
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `~gammapy.datasets.MapDataset`
            The resulting reduced dataset.
        """
        name = make_name(name)
        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}

        if not self.counts.geom.is_region:
            region_mask = (
                self.counts.geom.to_image().pad(1, axis_name=None).region_mask(region)
            )
            not_fully_contained = (
                np.any(region_mask.data[0, :])
                | np.any(region_mask.data[-1, :])
                | np.any(region_mask.data[:, 0])
                | np.any(region_mask.data[:, -1])
            )
            if not_fully_contained:
                raise Exception(
                    """`to_region_map_dataset` can only be applied if the region
                    is fully contained inside the counts geom.
                    """
                )

        if self.mask and not self.mask.geom.is_region:
            region_mask = self.mask.geom.to_image().region_mask(region)
            values = np.unique(self.mask.data[:, region_mask.data], axis=1)
            is_uniform = np.all(values, axis=1)
            is_uniform |= np.all(values == False, axis=1)  # noqa
            if not np.all(is_uniform):
                raise Exception(
                    """`to_region_map_dataset` can only be applied if the mask
                    is spatially uniform within the region for each energy bin"""
                )

        if self.mask_safe:
            kwargs["mask_safe"] = self.mask_safe.to_region_nd_map(region, func=np.any)

        if self.mask_fit:
            kwargs["mask_fit"] = self.mask_fit.to_region_nd_map(region, func=np.any)

        if self.counts:
            kwargs["counts"] = self.counts.to_region_nd_map(
                region, np.sum, weights=self.mask_safe
            )

        if self.stat_type == "cash" and self.background:
            kwargs["background"] = self.npred_background().to_region_nd_map(
                region, func=np.sum, weights=self.mask_safe
            )

        if self.exposure:
            kwargs["exposure"] = self.exposure.to_region_nd_map(region, func=np.mean)

        region = region.center if region else None

        # TODO: Compute average psf in region
        if self.psf:
            kwargs["psf"] = self.psf.to_region_nd_map(region)

        # TODO: Compute average edisp in region
        if self.edisp is not None:
            kwargs["edisp"] = self.edisp.to_region_nd_map(region)

        return self.__class__(**kwargs)

    def cutout(self, position, width, mode="trim", name=None):
        """Cutout map dataset.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Center position of the cutout region.
        width : tuple of `~astropy.coordinates.Angle`
            Angular sizes of the region in (lon, lat) in that specific order.
            If only one value is passed, a square region is extracted.
        mode : {'trim', 'partial', 'strict'}
            Mode option for Cutout2D, for details see `~astropy.nddata.utils.Cutout2D`. Default is "trim".
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        cutout : `MapDataset`
            Cutout map dataset.
        """
        name = make_name(name)
        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}
        cutout_kwargs = {"position": position, "width": width, "mode": mode}

        if self.counts is not None:
            kwargs["counts"] = self.counts.cutout(**cutout_kwargs)

        if self.exposure is not None:
            kwargs["exposure"] = self.exposure.cutout(**cutout_kwargs)

        if self.background is not None and self.stat_type == "cash":
            kwargs["background"] = self.background.cutout(**cutout_kwargs)

        if self.edisp is not None:
            kwargs["edisp"] = self.edisp.cutout(**cutout_kwargs)

        if self.psf is not None:
            kwargs["psf"] = self.psf.cutout(**cutout_kwargs)

        if self.mask_safe is not None:
            kwargs["mask_safe"] = self.mask_safe.cutout(**cutout_kwargs)

        if self.mask_fit is not None:
            kwargs["mask_fit"] = self.mask_fit.cutout(**cutout_kwargs)

        return self.__class__(**kwargs)

    def downsample(self, factor, axis_name=None, name=None):
        """Downsample map dataset.

        The PSFMap and EDispKernelMap are not downsampled, except if
        a corresponding axis is given.

        Parameters
        ----------
        factor : int
            Downsampling factor.
        axis_name : str, optional
            Which non-spatial axis to downsample. By default only spatial axes are downsampled. Default is None.
        name : str, optional
            Name of the downsampled dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            Downsampled map dataset.
        """
        name = make_name(name)

        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}

        if self.counts is not None:
            kwargs["counts"] = self.counts.downsample(
                factor=factor,
                preserve_counts=True,
                axis_name=axis_name,
                weights=self.mask_safe,
            )

        if self.exposure is not None:
            if axis_name is None:
                kwargs["exposure"] = self.exposure.downsample(
                    factor=factor, preserve_counts=False, axis_name=None
                )
            else:
                kwargs["exposure"] = self.exposure.copy()

        if self.background is not None and self.stat_type == "cash":
            kwargs["background"] = self.background.downsample(
                factor=factor, axis_name=axis_name, weights=self.mask_safe
            )

        if self.edisp is not None:
            if axis_name is not None:
                kwargs["edisp"] = self.edisp.downsample(
                    factor=factor, axis_name=axis_name, weights=self.mask_safe_edisp
                )
            else:
                kwargs["edisp"] = self.edisp.copy()

        if self.psf is not None:
            kwargs["psf"] = self.psf.copy()

        if self.mask_safe is not None:
            kwargs["mask_safe"] = self.mask_safe.downsample(
                factor=factor, preserve_counts=False, axis_name=axis_name
            )

        if self.mask_fit is not None:
            kwargs["mask_fit"] = self.mask_fit.downsample(
                factor=factor, preserve_counts=False, axis_name=axis_name
            )

        return self.__class__(**kwargs)

    def pad(self, pad_width, mode="constant", name=None):
        """Pad the spatial dimensions of the dataset.

        The padding only applies to counts, masks, background and exposure.

        Counts, background and masks are padded with zeros, exposure is padded with edge value.

        Parameters
        ----------
        pad_width : {sequence, array_like, int}
            Number of pixels padded to the edges of each axis.
        mode : str
            Pad mode. Default is "constant".
        name : str, optional
            Name of the padded dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset`
            Padded map dataset.

        """
        name = make_name(name)
        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}

        if self.counts is not None:
            kwargs["counts"] = self.counts.pad(pad_width=pad_width, mode=mode)

        if self.exposure is not None:
            kwargs["exposure"] = self.exposure.pad(pad_width=pad_width, mode=mode)

        if self.background is not None:
            kwargs["background"] = self.background.pad(pad_width=pad_width, mode=mode)

        if self.edisp is not None:
            kwargs["edisp"] = self.edisp.copy()

        if self.psf is not None:
            kwargs["psf"] = self.psf.copy()

        if self.mask_safe is not None:
            kwargs["mask_safe"] = self.mask_safe.pad(pad_width=pad_width, mode=mode)

        if self.mask_fit is not None:
            kwargs["mask_fit"] = self.mask_fit.pad(pad_width=pad_width, mode=mode)

        return self.__class__(**kwargs)

    def slice_by_idx(self, slices, name=None):
        """Slice sub dataset.

        The slicing only applies to the maps that define the corresponding axes.

        Parameters
        ----------
        slices : dict
            Dictionary of axes names and integers or `slice` object pairs. Contains one
            element for each non-spatial dimension. For integer indexing the
            corresponding axes is dropped from the map. Axes not specified in the
            dict are kept unchanged.
        name : str, optional
            Name of the sliced dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            Sliced dataset.

        Examples
        --------
        >>> from gammapy.datasets import MapDataset
        >>> dataset = MapDataset.read("$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz")
        >>> slices = {"energy": slice(0, 3)} #to get the first 3 energy slices
        >>> sliced = dataset.slice_by_idx(slices)
        >>> print(sliced.geoms["geom"])
        WcsGeom
        <BLANKLINE>
            axes       : ['lon', 'lat', 'energy']
            shape      : (320, 240, 3)
            ndim       : 3
            frame      : galactic
            projection : CAR
            center     : 0.0 deg, 0.0 deg
            width      : 8.0 deg x 6.0 deg
            wcs ref    : 0.0 deg, 0.0 deg
        <BLANKLINE>
        """
        name = make_name(name)
        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}

        if self.counts is not None:
            kwargs["counts"] = self.counts.slice_by_idx(slices=slices)

        if self.exposure is not None:
            kwargs["exposure"] = self.exposure.slice_by_idx(slices=slices)

        if self.background is not None and self.stat_type == "cash":
            kwargs["background"] = self.background.slice_by_idx(slices=slices)

        if self.edisp is not None:
            kwargs["edisp"] = self.edisp.slice_by_idx(slices=slices)

        if self.psf is not None:
            kwargs["psf"] = self.psf.slice_by_idx(slices=slices)

        if self.mask_safe is not None:
            kwargs["mask_safe"] = self.mask_safe.slice_by_idx(slices=slices)

        if self.mask_fit is not None:
            kwargs["mask_fit"] = self.mask_fit.slice_by_idx(slices=slices)

        return self.__class__(**kwargs)

    def slice_by_energy(self, energy_min=None, energy_max=None, name=None):
        """Select and slice datasets in energy range.

        Parameters
        ----------
        energy_min, energy_max : `~astropy.units.Quantity`, optional
            Energy bounds to compute the flux point for. Default is None.
        name : str, optional
            Name of the sliced dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset`
            Sliced Dataset.

        Examples
        --------
        >>> from gammapy.datasets import MapDataset
        >>> dataset = MapDataset.read("$GAMMAPY_DATA/cta-1dc-gc/cta-1dc-gc.fits.gz")
        >>> sliced = dataset.slice_by_energy(energy_min="1 TeV", energy_max="5 TeV")
        >>> sliced.data_shape
        (3, 240, 320)
        """
        name = make_name(name)

        energy_axis = self._geom.axes["energy"]

        if energy_min is None:
            energy_min = energy_axis.bounds[0]

        if energy_max is None:
            energy_max = energy_axis.bounds[1]

        energy_min, energy_max = u.Quantity(energy_min), u.Quantity(energy_max)

        group = energy_axis.group_table(edges=[energy_min, energy_max])

        is_normal = group["bin_type"] == "normal   "
        group = group[is_normal]

        slices = {
            "energy": slice(int(group["idx_min"][0]), int(group["idx_max"][0]) + 1)
        }

        return self.slice_by_idx(slices, name=name)

    def reset_data_cache(self):
        """Reset data cache to free memory space."""
        for name in self._lazy_data_members:
            if self.__dict__.pop(name, False):
                log.info(f"Clearing {name} cache for dataset {self.name}")

    def resample_energy_axis(self, energy_axis, name=None):
        """Resample MapDataset over new reco energy axis.

        Counts are summed taking into account safe mask.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            New reconstructed energy axis.
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            Resampled dataset.
        """
        name = make_name(name)
        kwargs = {"gti": self.gti, "name": name, "meta_table": self.meta_table}

        if self.exposure:
            kwargs["exposure"] = self.exposure

        if self.psf:
            kwargs["psf"] = self.psf

        if self.mask_safe is not None:
            kwargs["mask_safe"] = self.mask_safe.resample_axis(
                axis=energy_axis, ufunc=np.logical_or
            )

        if self.mask_fit is not None:
            kwargs["mask_fit"] = self.mask_fit.resample_axis(
                axis=energy_axis, ufunc=np.logical_or
            )

        if self.counts is not None:
            kwargs["counts"] = self.counts.resample_axis(
                axis=energy_axis, weights=self.mask_safe
            )

        if self.background is not None and self.stat_type == "cash":
            kwargs["background"] = self.background.resample_axis(
                axis=energy_axis, weights=self.mask_safe
            )

        # Mask_safe or mask_irf??
        if isinstance(self.edisp, EDispKernelMap):
            kwargs["edisp"] = self.edisp.resample_energy_axis(
                energy_axis=energy_axis, weights=self.mask_safe_edisp
            )
        else:  # None or EDispMap
            kwargs["edisp"] = self.edisp

        return self.__class__(**kwargs)

    def to_image(self, name=None):
        """Create images by summing over the reconstructed energy axis.

        Parameters
        ----------
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset` or `SpectrumDataset`
            Dataset integrated over non-spatial axes.
        """
        energy_axis = self._geom.axes["energy"].squash()
        return self.resample_energy_axis(energy_axis=energy_axis, name=name)

    def peek(self, figsize=(13.0, 7)):
        """Quick-look summary plots for a given MapDataset:
        - Exposure map
        - Counts map
        - Predicted counts map (Npred)
        - Exposure profile at geom center
        - PSF containment radius at geom center
        - Energy dispersion matrix at geom center

        Parameters
        ----------
        figsize : tuple
            Size of the figure. Default is (13.5, 7).

        """

        def plot_counts(ax, counts_data, cmap, vmin, vmax, title="Counts map"):
            counts_data.plot(
                ax=ax,
                cmap=cmap,
                add_cbar=True,
                interpolation="bilinear",
                norm=LogNorm(vmin=vmin, vmax=vmax),
            )
            ax.set_title(title)
            ax.set_box_aspect(1)

        def plot_edisp(ax, edisp_kernel):
            edisp_kernel.plot_matrix(ax=ax, add_cbar=False)
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_title("Energy Dispersion (at FoV center)")
            ax.set_box_aspect(1)

        def plot_exposure_map(ax, exposure_map, cmap):
            index = int(exposure_map.geom.axes[0].nbin / 2)

            # Dynamically scale the exposure by powers of 10 for improved readability
            exp_data = exposure_map.get_image_by_idx([index])
            vmin = exp_data.data[exp_data.data > 0].min()
            vmax = exp_data.data[exp_data.data > 0].max()

            energy_center = exposure_map.geom.axes[0].center[index]

            exp_data.plot(
                ax=ax,
                cmap=cmap,
                norm=LogNorm(vmin=vmin, vmax=vmax),
                add_cbar=True,
            )

            unit = exposure_map.unit.to_string("latex")
            cbar = ax.images[-1].colorbar  # Access the colorbar
            cbar.set_label(f"Exposure [{unit}]")  # Set the formatted label

            if energy_center.value < 1e-2 or energy_center.value > 1e2:
                title = f"Exposure map at {energy_center:.1e}"
            elif energy_center.value < 1e-1 or energy_center.value > 1e1:
                title = f"Exposure map at {energy_center:.1f}"
            else:
                title = f"Exposure map at {energy_center:.2f}"

            ax.set_title(title)
            ax.set_box_aspect(1)

        def plot_exposure_profile(ax, exposure_map):
            exposure_map.plot(ax=ax, ls="solid", marker=None, xerr=None)
            # Dynamically format the y-axis label
            unit = exposure_map.unit.to_string("latex")  # Convert unit to LaTeX format
            ax.set_ylabel(f"Exposure [{unit}]")  # Set the formatted y-axis label
            ax.set_title("Exposure (at FoV center)")
            ax.set_box_aspect(1)

        def plot_containment_radius(ax, psf):
            psf.plot_containment_radius_vs_energy(ax=ax)
            ax.legend(fontsize="small")
            ax.set_title("Containment radius (at FoV center)")
            ax.set_xscale("log")
            ax.set_yscale("log")
            ax.set_box_aspect(1)

        def plot_mask(ax, mask, **kwargs):
            if mask is not None:
                mask.plot_mask(ax=ax, **kwargs)

        # Reduce the datasets to 2D if needed
        countsmapdata = self.counts.reduce_over_axes()
        npredmapdata = self.npred().reduce_over_axes()

        # Get the corresponding central pixel SpectrumDataset (exposure, edisp, psf)
        central_pixel = RectangleSkyRegion(
            self.counts.geom.center_skydir,
            width=1.01 * self.counts.geom.pixel_scales[0],
            height=1.01 * self.counts.geom.pixel_scales[1],
        )
        central_spectrum_dataset = self.to_spectrum_dataset(central_pixel)

        # Determine plotting limits
        vmin = npredmapdata.data.min()
        vmax = npredmapdata.data.max()
        # Fallback if the map is entirely zero
        if vmin == 0.0:
            vmin = np.max([countsmapdata.data.max() * 0.02, countsmapdata.data.min()])
        if vmax == 0.0:
            vmax = countsmapdata.data.max()

        # Create custom colormaps
        cmapcustom = plt.get_cmap("afmhot")
        cmapcustom.set_bad(color="black")

        # Create the figure and axes
        fig, axs = plt.subplots(nrows=2, ncols=3, figsize=figsize)

        # --- Plot Exposure Map ---
        axs[0, 0].remove()
        ax_exposure = fig.add_subplot(2, 3, 1, projection=self.exposure.geom.wcs)
        plot_exposure_map(ax_exposure, self.exposure, cmap=cmapcustom)
        plot_mask(
            ax=ax_exposure, mask=self.mask_safe_image, hatches=["///"], colors="w"
        )

        # --- Plot Counts Map ---
        axs[0, 1].remove()
        ax_counts = fig.add_subplot(2, 3, 2, projection=self.counts.geom.wcs)
        plot_counts(ax_counts, countsmapdata, cmapcustom, vmin, vmax, "Counts map")
        plot_mask(ax=ax_counts, mask=self.mask_fit_image, alpha=0.2)
        plot_mask(ax=ax_counts, mask=self.mask_safe_image, hatches=["///"], colors="w")

        # --- Plot npred Map ---
        axs[0, 2].remove()
        ax_npred = fig.add_subplot(2, 3, 3, projection=self.npred().geom.wcs)
        plot_counts(ax_npred, npredmapdata, cmapcustom, vmin, vmax, "Model npred")
        plot_mask(ax=ax_npred, mask=self.mask_fit_image, alpha=0.2)
        plot_mask(ax=ax_npred, mask=self.mask_safe_image, hatches=["///"], colors="w")

        # --- Plot Exposure Profile ---
        ax_exp_profile = axs[1, 0]
        plot_exposure_profile(ax_exp_profile, central_spectrum_dataset.exposure)

        # --- Plot Containment Radius ---
        ax_containment = axs[1, 1]
        plot_containment_radius(ax_containment, self.psf)

        # --- Plot Energy Dispersion ---
        ax_edisp = axs[1, 2]
        plot_edisp(ax_edisp, central_spectrum_dataset.edisp.get_edisp_kernel())

        plt.tight_layout(w_pad=0)


class MapDatasetOnOff(MapDataset):
    """Map dataset for on-off likelihood fitting.

    It bundles together the binned on and off counts, the binned IRFs as well as the on and off acceptances.

    A safe mask and a fit mask can be added to exclude bins during the analysis.

    It uses the Wstat statistic (see `~gammapy.stats.wstat`), therefore no background model is needed.

    For more information see :ref:`datasets`.

    Parameters
    ----------
    models : `~gammapy.modeling.models.Models`
        Source sky models.
    counts : `~gammapy.maps.WcsNDMap`
        Counts cube.
    counts_off : `~gammapy.maps.WcsNDMap`
        Ring-convolved counts cube.
    acceptance : `~gammapy.maps.WcsNDMap`
        Acceptance from the IRFs.
    acceptance_off : `~gammapy.maps.WcsNDMap`
        Acceptance off.
    exposure : `~gammapy.maps.WcsNDMap`
        Exposure cube.
    mask_fit : `~gammapy.maps.WcsNDMap`
        Mask to apply to the likelihood for fitting.
    psf : `~gammapy.irf.PSFKernel`
        PSF kernel.
    edisp : `~gammapy.irf.EDispKernel`
        Energy dispersion.
    mask_safe : `~gammapy.maps.WcsNDMap`
        Mask defining the safe data range.
    gti : `~gammapy.data.GTI`
        GTI of the observation or union of GTI if it is a stacked observation.
    meta_table : `~astropy.table.Table`
        Table listing information on observations used to create the dataset.
        One line per observation for stacked datasets.
    name : str
        Name of the dataset.
    meta : `~gammapy.datasets.MapDatasetMetaData`
        Associated meta data container


    See Also
    --------
    MapDataset, SpectrumDataset, FluxPointsDataset.

    """

    tag = "MapDatasetOnOff"

    def __init__(
        self,
        models=None,
        counts=None,
        counts_off=None,
        acceptance=None,
        acceptance_off=None,
        exposure=None,
        mask_fit=None,
        psf=None,
        edisp=None,
        name=None,
        mask_safe=None,
        gti=None,
        meta_table=None,
        meta=None,
        stat_type="wstat",
    ):
        self._name = make_name(name)
        self._evaluators = {}

        self.counts = counts
        self.counts_off = counts_off
        self.exposure = exposure
        self.acceptance = acceptance
        self.acceptance_off = acceptance_off
        self.gti = gti
        self.mask_fit = mask_fit
        self.psf = psf
        self.edisp = edisp
        self.models = models
        self.mask_safe = mask_safe
        self.meta_table = meta_table
        if meta is None:
            self._meta = MapDatasetMetaData()
        else:
            self._meta = meta

        self.stat_type = stat_type

    def __str__(self):
        str_ = super().__str__()

        if self.mask_safe:
            mask = self.mask_safe.data.astype(bool)
        else:
            mask = slice(None)

        counts_off = np.nan
        if self.counts_off is not None:
            counts_off = np.sum(self.counts_off.data[mask])
        str_ += "\t{:32}: {:.0f} \n".format("Total counts_off", counts_off)

        acceptance = np.nan
        if self.acceptance is not None:
            acceptance = np.sum(self.acceptance.data[mask])
        str_ += "\t{:32}: {:.0f} \n".format("Acceptance", acceptance)

        acceptance_off = np.nan
        if self.acceptance_off is not None:
            acceptance_off = np.sum(self.acceptance_off.data[mask])
        str_ += "\t{:32}: {:.0f} \n".format("Acceptance off", acceptance_off)

        return str_.expandtabs(tabsize=2)

    @property
    def _geom(self):
        """Main analysis geometry."""
        if self.counts is not None:
            return self.counts.geom
        elif self.counts_off is not None:
            return self.counts_off.geom
        elif self.acceptance is not None:
            return self.acceptance.geom
        elif self.acceptance_off is not None:
            return self.acceptance_off.geom
        else:
            raise ValueError(
                "Either 'counts', 'counts_off', 'acceptance' or 'acceptance_of' must be defined."
            )

    @property
    def alpha(self):
        """Exposure ratio between signal and background regions.

        See :ref:`wstat`.

        Returns
        -------
        alpha : `Map`
            Alpha map.
        """
        with np.errstate(invalid="ignore", divide="ignore"):
            data = self.acceptance.quantity / self.acceptance_off.quantity
        data = np.nan_to_num(data)

        return Map.from_geom(self._geom, data=data.to_value(""), unit="")

    def npred_background(self):
        """Predicted background counts estimated from the marginalized likelihood estimate.

        See :ref:`wstat`.

        Returns
        -------
        npred_background : `Map`
            Predicted background counts.
        """
        mu_bkg = self.alpha.data * get_wstat_mu_bkg(
            n_on=self.counts.data,
            n_off=self.counts_off.data,
            alpha=self.alpha.data,
            mu_sig=self.npred_signal().data,
        )
        mu_bkg = np.nan_to_num(mu_bkg)
        return Map.from_geom(geom=self._geom, data=mu_bkg)

    def npred_off(self):
        """Predicted counts in the off region; mu_bkg/alpha.

        See :ref:`wstat`.

        Returns
        -------
        npred_off : `Map`
            Predicted off counts.
        """
        return self.npred_background() / self.alpha

    @property
    def background(self):
        """Computed as alpha * n_off.

        See :ref:`wstat`.

        Returns
        -------
        background : `Map`
            Background map.
        """
        if self.counts_off is None:
            return None
        return self.alpha * self.counts_off

    @property
    def _counts_statistic(self):
        """Counts statistics of the dataset."""
        return WStatCountsStatistic(self.counts, self.counts_off, self.alpha)

    @classmethod
    def from_geoms(
        cls,
        geom,
        geom_exposure=None,
        geom_psf=None,
        geom_edisp=None,
        reference_time="2000-01-01",
        name=None,
        **kwargs,
    ):
        """Create an empty `MapDatasetOnOff` object according to the specified geometries.

        Parameters
        ----------
        geom : `gammapy.maps.WcsGeom`
            Geometry for the counts, counts_off, acceptance and acceptance_off maps.
        geom_exposure : `gammapy.maps.WcsGeom`, optional
            Geometry for the exposure map. Default is None.
        geom_psf : `gammapy.maps.WcsGeom`, optional
            Geometry for the PSF map. Default is None.
        geom_edisp : `gammapy.maps.WcsGeom`, optional
            Geometry for the energy dispersion kernel map.
            If geom_edisp has a migra axis, this will create an EDispMap instead. Default is None.
        reference_time : `~astropy.time.Time`
            The reference time to use in GTI definition. Default is "2000-01-01".
        name : str, optional
            Name of the returned dataset. Default is None.
        **kwargs : dict, optional
            Keyword arguments to be passed.

        Returns
        -------
        empty_maps : `MapDatasetOnOff`
            A MapDatasetOnOff containing zero filled maps.
        """
        #  TODO: it seems the super() pattern does not work here?
        dataset = MapDataset.from_geoms(
            geom=geom,
            geom_exposure=geom_exposure,
            geom_psf=geom_psf,
            geom_edisp=geom_edisp,
            name=name,
            reference_time=reference_time,
            **kwargs,
        )

        off_maps = {}

        for key in ["counts_off", "acceptance", "acceptance_off"]:
            off_maps[key] = Map.from_geom(geom, unit="")

        return cls.from_map_dataset(dataset, name=name, **off_maps)

    @classmethod
    def from_map_dataset(
        cls, dataset, acceptance, acceptance_off, counts_off=None, name=None
    ):
        """Create on off dataset from a map dataset.

        Parameters
        ----------
        dataset : `MapDataset`
            Spectrum dataset defining counts, edisp, aeff, livetime etc.
        acceptance : `Map`
            Relative background efficiency in the on region.
        acceptance_off : `Map`
            Relative background efficiency in the off region.
        counts_off : `Map`, optional
            Off counts map . If the dataset provides a background model,
            and no off counts are defined. The off counts are deferred from
            counts_off / alpha. Default is None.
        name : str, optional
            Name of the returned dataset. Default is None.

        Returns
        -------
        dataset : `MapDatasetOnOff`
            Map dataset on off.

        """
        if counts_off is None and dataset.background is not None:
            alpha = acceptance / acceptance_off
            counts_off = dataset.npred_background() / alpha

        if np.isscalar(acceptance):
            acceptance = Map.from_geom(dataset._geom, data=acceptance)

        if np.isscalar(acceptance_off):
            acceptance_off = Map.from_geom(dataset._geom, data=acceptance_off)

        return cls(
            models=dataset.models,
            counts=dataset.counts,
            exposure=dataset.exposure,
            counts_off=counts_off,
            edisp=dataset.edisp,
            psf=dataset.psf,
            mask_safe=dataset.mask_safe,
            mask_fit=dataset.mask_fit,
            acceptance=acceptance,
            acceptance_off=acceptance_off,
            gti=dataset.gti,
            name=name,
            meta_table=dataset.meta_table,
        )

    def to_map_dataset(self, name=None):
        """Convert a MapDatasetOnOff to a MapDataset.

        The background model template is taken as alpha * counts_off.

        Parameters
        ----------
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `MapDataset`
            Map dataset with cash statistics.
        """
        name = make_name(name)

        background = self.counts_off * self.alpha if self.counts_off else None

        return MapDataset(
            counts=self.counts,
            exposure=self.exposure,
            psf=self.psf,
            edisp=self.edisp,
            name=name,
            gti=self.gti,
            mask_fit=self.mask_fit,
            mask_safe=self.mask_safe,
            background=background,
            meta_table=self.meta_table,
        )

    def _to_asimov_dataset(self):
        """Create Asimov dataset from the current models."""
        npred = self.npred()
        data = np.nan_to_num(npred.data, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        npred.data = data.astype("float")

        asimov_dataset = self.__class__(
            models=self.models,
            counts=npred,
            counts_off=self.counts_off,
            exposure=self.exposure,
            acceptance=self.acceptance,
            acceptance_off=self.acceptance_off,
            psf=self.psf,
            edisp=self.edisp,
            mask_safe=self.mask_safe,
            mask_fit=self.mask_fit,
            gti=self.gti,
            name=self.name,
            meta=self.meta,
        )
        asimov_dataset._evaluators = self._evaluators

        return asimov_dataset

    @property
    def _is_stackable(self):
        """Check if the Dataset contains enough information to be stacked."""
        incomplete = (
            self.acceptance_off is None
            or self.acceptance is None
            or self.counts_off is None
        )
        unmasked = np.any(self.mask_safe.data)
        if incomplete and unmasked:
            return False
        else:
            return True

    def stack(self, other, nan_to_num=True):
        r"""Stack another dataset in place.

        Safe mask is applied to the other dataset to compute the stacked counts data,
        counts outside the safe mask are lost (as for `~gammapy.MapDataset.stack`).

        The ``acceptance`` of the stacked dataset is obtained by stacking the acceptance weighted
        by the other mask_safe onto the current unweighted acceptance.

        Note that the masking is not applied to the current dataset. If masking needs
        to be applied to it, use `~gammapy.MapDataset.to_masked()` first.

        The stacked ``acceptance_off`` is scaled so that:

        .. math::
            \alpha_\text{stacked} =
            \frac{1}{a_\text{off}} =
            \frac{\alpha_1\text{OFF}_1 + \alpha_2\text{OFF}_2}{\text{OFF}_1 + OFF_2}.

        For details, see :ref:`stack`.

        Parameters
        ----------
        other : `MapDatasetOnOff`
            Other dataset.
        nan_to_num : bool
            Non-finite values are replaced by zero if True. Default is True.
        """
        if not isinstance(other, MapDatasetOnOff):
            raise TypeError("Incompatible types for MapDatasetOnOff stacking")

        if not self._is_stackable or not other._is_stackable:
            raise ValueError("Cannot stack incomplete MapDatasetOnOff.")

        geom = self.counts.geom
        total_off = Map.from_geom(geom)
        total_alpha = Map.from_geom(geom)
        total_acceptance = Map.from_geom(geom)

        total_acceptance.stack(self.acceptance, nan_to_num=nan_to_num)
        total_acceptance.stack(
            other.acceptance, weights=other.mask_safe, nan_to_num=nan_to_num
        )

        if self.counts_off:
            total_off.stack(self.counts_off, nan_to_num=nan_to_num)
            total_alpha.stack(self.alpha * self.counts_off, nan_to_num=nan_to_num)
        if other.counts_off:
            total_off.stack(
                other.counts_off, weights=other.mask_safe, nan_to_num=nan_to_num
            )
            total_alpha.stack(
                other.alpha * other.counts_off,
                weights=other.mask_safe,
                nan_to_num=nan_to_num,
            )

        with np.errstate(divide="ignore", invalid="ignore"):
            acceptance_off = total_acceptance * total_off / total_alpha
            average_alpha = total_alpha.data.sum() / total_off.data.sum()

        # For the bins where the stacked OFF counts equal 0, the alpha value is
        # performed by weighting on the total OFF counts of each run
        is_zero = total_off.data == 0
        acceptance_off.data[is_zero] = total_acceptance.data[is_zero] / average_alpha

        self.acceptance.data[...] = total_acceptance.data
        self.acceptance_off = acceptance_off

        self.counts_off = total_off

        super().stack(other, nan_to_num=nan_to_num)

    def fake(self, npred_background, random_state="random-seed"):
        """Simulate fake counts (on and off) for the current model and reduced IRFs.

        This method overwrites the counts defined on the dataset object.

        Parameters
        ----------
        npred_background : `~gammapy.maps.Map`
                Expected number of background counts in the on region.
        random_state : {int, 'random-seed', 'global-rng', `~numpy.random.RandomState`}
                Defines random number generator initialisation.
                Passed to `~gammapy.utils.random.get_random_state`. Default is "random-seed".
        """
        random_state = get_random_state(random_state)
        npred = self.npred_signal()
        data = np.nan_to_num(npred.data, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        npred.data = random_state.poisson(data)

        npred_bkg = random_state.poisson(npred_background.data)

        self.counts = npred + npred_bkg

        npred_off = npred_background / self.alpha
        data_off = np.nan_to_num(
            npred_off.data, copy=True, nan=0.0, posinf=0.0, neginf=0.0
        )
        npred_off.data = random_state.poisson(data_off)
        self.counts_off = npred_off

    def to_hdulist(self):
        """Convert map dataset to list of HDUs.

        Returns
        -------
        hdulist : `~astropy.io.fits.HDUList`
            Map dataset list of HDUs.
        """
        hdulist = super().to_hdulist()
        exclude_primary = slice(1, None)

        creation = self.meta.creation

        del hdulist["BACKGROUND"]
        del hdulist["BACKGROUND_BANDS"]

        if self.counts_off is not None:
            hdulist += self.counts_off.to_hdulist(hdu="counts_off")[exclude_primary]

        if self.acceptance is not None:
            hdulist += self.acceptance.to_hdulist(hdu="acceptance")[exclude_primary]

        if self.acceptance_off is not None:
            hdulist += self.acceptance_off.to_hdulist(hdu="acceptance_off")[
                exclude_primary
            ]

        for hdu in hdulist:
            hdu.header.update(creation.to_header())

        return hdulist

    @classmethod
    def _read_lazy(cls, filename, name=None, cache=True, format="gadf"):
        raise NotImplementedError(
            f"Lazy loading is not implemented for {cls}, please use option lazy=False."
        )

    @classmethod
    def from_hdulist(cls, hdulist, name=None, format="gadf"):
        """Create map dataset from list of HDUs.

        Parameters
        ----------
        hdulist : `~astropy.io.fits.HDUList`
            List of HDUs.
        name : str, optional
            Name of the new dataset. Default is None.
        format : {"gadf"}
            Format the hdulist is given in. Default is "gadf".

        Returns
        -------
        dataset : `MapDatasetOnOff`
            Map dataset.
        """
        kwargs = {}
        kwargs["name"] = name

        if "COUNTS" in hdulist:
            kwargs["counts"] = Map.from_hdulist(hdulist, hdu="counts", format=format)

        if "COUNTS_OFF" in hdulist:
            kwargs["counts_off"] = Map.from_hdulist(
                hdulist, hdu="counts_off", format=format
            )

        if "ACCEPTANCE" in hdulist:
            kwargs["acceptance"] = Map.from_hdulist(
                hdulist, hdu="acceptance", format=format
            )

        if "ACCEPTANCE_OFF" in hdulist:
            kwargs["acceptance_off"] = Map.from_hdulist(
                hdulist, hdu="acceptance_off", format=format
            )

        if "EXPOSURE" in hdulist:
            kwargs["exposure"] = Map.from_hdulist(
                hdulist, hdu="exposure", format=format
            )

        if "EDISP" in hdulist:
            edisp_map = Map.from_hdulist(hdulist, hdu="edisp", format=format)

            try:
                exposure_map = Map.from_hdulist(
                    hdulist, hdu="edisp_exposure", format=format
                )
            except KeyError:
                exposure_map = None

            if edisp_map.geom.axes[0].name == "energy":
                kwargs["edisp"] = EDispKernelMap(edisp_map, exposure_map)
            else:
                kwargs["edisp"] = EDispMap(edisp_map, exposure_map)

        if "PSF" in hdulist:
            psf_map = Map.from_hdulist(hdulist, hdu="psf", format=format)
            try:
                exposure_map = Map.from_hdulist(
                    hdulist, hdu="psf_exposure", format=format
                )
            except KeyError:
                exposure_map = None
            kwargs["psf"] = PSFMap(psf_map, exposure_map)

        if "MASK_SAFE" in hdulist:
            mask_safe = Map.from_hdulist(hdulist, hdu="mask_safe", format=format)
            kwargs["mask_safe"] = mask_safe

        if "MASK_FIT" in hdulist:
            mask_fit = Map.from_hdulist(hdulist, hdu="mask_fit", format=format)
            kwargs["mask_fit"] = mask_fit

        if "GTI" in hdulist:
            gti = GTI.from_table_hdu(hdulist["GTI"])
            kwargs["gti"] = gti

        if "META_TABLE" in hdulist:
            meta_table = Table.read(hdulist, hdu="META_TABLE")
            kwargs["meta_table"] = meta_table
        return cls(**kwargs)

    def info_dict(self, in_safe_data_range=True):
        """Basic info dict with summary statistics.

        If a region is passed, then a spectrum dataset is
        extracted, and the corresponding info returned.

        Parameters
        ----------
        in_safe_data_range : bool
            Whether to sum only in the safe energy range. Default is True.

        Returns
        -------
        info_dict : dict
            Dictionary with summary info.
        """
        # TODO: remove code duplication with SpectrumDatasetOnOff
        info = super().info_dict(in_safe_data_range)

        if self.mask_safe and in_safe_data_range:
            mask = self.mask_safe.data.astype(bool)
        else:
            mask = slice(None)

        summed_stat = self._counts_statistic[mask].sum()

        counts_off = 0
        if self.counts_off is not None:
            counts_off = summed_stat.n_off

        info["counts_off"] = int(counts_off)

        acceptance = 1
        if self.acceptance:
            acceptance = self.acceptance.data[mask].sum()

        info["acceptance"] = float(acceptance)

        acceptance_off = np.nan
        alpha = np.nan

        if self.acceptance_off:
            alpha = summed_stat.alpha
            acceptance_off = acceptance / alpha

        info["acceptance_off"] = float(acceptance_off)
        info["alpha"] = float(alpha)

        info["stat_sum"] = self.stat_sum()
        return info

    def to_spectrum_dataset(self, on_region, containment_correction=False, name=None):
        """Return a ~gammapy.datasets.SpectrumDatasetOnOff from on_region.

        Counts and OFF counts are summed in the on_region.

        Acceptance is the average of all acceptances while acceptance OFF
        is taken such that number of excess is preserved in the on_region.

        Effective area is taken from the average exposure.

        The energy dispersion kernel is obtained at the on_region center.
        Only regions with centers are supported.

        The models are not exported to the ~gammapy.dataset.SpectrumDatasetOnOff.
        It must be set after the dataset extraction.

        Parameters
        ----------
        on_region : `~regions.SkyRegion`
            The input ON region on which to extract the spectrum.
        containment_correction : bool
            Apply containment correction for point sources and circular on regions. Default is False.
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `~gammapy.datasets.SpectrumDatasetOnOff`
            The resulting reduced dataset.
        """
        from .spectrum import SpectrumDatasetOnOff

        dataset = super().to_spectrum_dataset(
            on_region=on_region,
            containment_correction=containment_correction,
            name=name,
        )

        kwargs = {"name": name}

        if self.counts_off is not None:
            kwargs["counts_off"] = self.counts_off.get_spectrum(
                on_region, np.sum, weights=self.mask_safe
            )

        if self.acceptance is not None:
            kwargs["acceptance"] = self.acceptance.get_spectrum(
                on_region, np.mean, weights=self.mask_safe
            )
            norm = self.background.get_spectrum(
                on_region, np.sum, weights=self.mask_safe
            )
            acceptance_off = kwargs["acceptance"] * kwargs["counts_off"] / norm
            np.nan_to_num(acceptance_off.data, copy=False)
            kwargs["acceptance_off"] = acceptance_off

        return SpectrumDatasetOnOff.from_spectrum_dataset(dataset=dataset, **kwargs)

    def cutout(self, position, width, mode="trim", name=None):
        """Cutout map dataset.

        Parameters
        ----------
        position : `~astropy.coordinates.SkyCoord`
            Center position of the cutout region.
        width : tuple of `~astropy.coordinates.Angle`
            Angular sizes of the region in (lon, lat) in that specific order.
            If only one value is passed, a square region is extracted.
        mode : {'trim', 'partial', 'strict'}
            Mode option for Cutout2D, for details see `~astropy.nddata.utils.Cutout2D`. Default is "trim".
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        cutout : `MapDatasetOnOff`
            Cutout map dataset.
        """
        cutout_kwargs = {
            "position": position,
            "width": width,
            "mode": mode,
            "name": name,
        }

        cutout_dataset = super().cutout(**cutout_kwargs)

        del cutout_kwargs["name"]

        if self.counts_off is not None:
            cutout_dataset.counts_off = self.counts_off.cutout(**cutout_kwargs)

        if self.acceptance is not None:
            cutout_dataset.acceptance = self.acceptance.cutout(**cutout_kwargs)

        if self.acceptance_off is not None:
            cutout_dataset.acceptance_off = self.acceptance_off.cutout(**cutout_kwargs)

        return cutout_dataset

    def downsample(self, factor, axis_name=None, name=None):
        """Downsample map dataset.

        The PSFMap and EDispKernelMap are not downsampled, except if
        a corresponding axis is given.

        Parameters
        ----------
        factor : int
            Downsampling factor.
        axis_name : str, optional
            Which non-spatial axis to downsample. By default, only spatial axes are downsampled. Default is None.
        name : str, optional
            Name of the downsampled dataset. Default is None.

        Returns
        -------
        dataset : `MapDatasetOnOff`
            Downsampled map dataset.
        """

        dataset = super().downsample(factor, axis_name, name)

        counts_off = None
        if self.counts_off is not None:
            counts_off = self.counts_off.downsample(
                factor=factor,
                preserve_counts=True,
                axis_name=axis_name,
                weights=self.mask_safe,
            )

        acceptance, acceptance_off = None, None
        if self.acceptance_off is not None:
            acceptance = self.acceptance.downsample(
                factor=factor, preserve_counts=False, axis_name=axis_name
            )
            factor = self.background.downsample(
                factor=factor,
                preserve_counts=True,
                axis_name=axis_name,
                weights=self.mask_safe,
            )
            acceptance_off = acceptance * counts_off / factor

        return self.__class__.from_map_dataset(
            dataset,
            acceptance=acceptance,
            acceptance_off=acceptance_off,
            counts_off=counts_off,
        )

    def pad(self):
        """Not implemented for MapDatasetOnOff."""
        raise NotImplementedError

    def slice_by_idx(self, slices, name=None):
        """Slice sub dataset.

        The slicing only applies to the maps that define the corresponding axes.

        Parameters
        ----------
        slices : dict
            Dictionary of axes names and integers or `slice` object pairs. Contains one
            element for each non-spatial dimension. For integer indexing the
            corresponding axes is dropped from the map. Axes not specified in the
            dict are kept unchanged.
        name : str, optional
            Name of the sliced dataset. Default is None.

        Returns
        -------
        map_out : `Map`
            Sliced map object.
        """
        kwargs = {"name": name}
        dataset = super().slice_by_idx(slices, name)

        if self.counts_off is not None:
            kwargs["counts_off"] = self.counts_off.slice_by_idx(slices=slices)

        if self.acceptance is not None:
            kwargs["acceptance"] = self.acceptance.slice_by_idx(slices=slices)

        if self.acceptance_off is not None:
            kwargs["acceptance_off"] = self.acceptance_off.slice_by_idx(slices=slices)

        return self.from_map_dataset(dataset, **kwargs)

    def resample_energy_axis(self, energy_axis, name=None):
        """Resample MapDatasetOnOff over reconstructed energy edges.

        Counts are summed taking into account safe mask.

        Parameters
        ----------
        energy_axis : `~gammapy.maps.MapAxis`
            New reco energy axis.
        name : str, optional
            Name of the new dataset. Default is None.

        Returns
        -------
        dataset : `SpectrumDataset`
            Resampled spectrum dataset.
        """
        dataset = super().resample_energy_axis(energy_axis, name)

        counts_off = None
        if self.counts_off is not None:
            counts_off = self.counts_off
            counts_off = counts_off.resample_axis(
                axis=energy_axis, weights=self.mask_safe
            )

        acceptance = 1
        acceptance_off = None
        if self.acceptance is not None:
            acceptance = self.acceptance
            acceptance = acceptance.resample_axis(
                axis=energy_axis, weights=self.mask_safe
            )

            norm_factor = self.background.resample_axis(
                axis=energy_axis, weights=self.mask_safe
            )

            acceptance_off = acceptance * counts_off / norm_factor

        return self.__class__.from_map_dataset(
            dataset,
            acceptance=acceptance,
            acceptance_off=acceptance_off,
            counts_off=counts_off,
            name=name,
        )
