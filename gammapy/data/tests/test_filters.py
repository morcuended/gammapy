# Licensed under a 3-clause BSD style license - see LICENSE.rst
import pytest
import numpy as np
from astropy import units as u
from astropy.coordinates import Angle, SkyCoord
from astropy.time import Time
from astropy.units import Quantity
from gammapy.data import GTI, DataStore, EventList, ObservationFilter
from gammapy.utils.regions import SphericalCircleSkyRegion
from gammapy.utils.testing import assert_allclose, assert_time_allclose, requires_data


def test_event_filter_types():
    for method_str in ObservationFilter.EVENT_FILTER_TYPES.values():
        assert hasattr(EventList, method_str)


@pytest.fixture(scope="session")
def observation():
    ds = DataStore.from_dir("$GAMMAPY_DATA/hess-dl3-dr1/")
    return ds.obs(20136)


@requires_data()
def test_empty_observation_filter(observation):
    empty_obs_filter = ObservationFilter()

    events = observation.events
    filtered_events = empty_obs_filter.filter_events(events)
    assert filtered_events == events

    gti = observation.gti
    filtered_gti = empty_obs_filter.filter_gti(gti)
    assert filtered_gti == gti


@requires_data()
def test_filter_events(observation):
    custom_filter = {
        "type": "custom",
        "opts": {
            "parameter": "ENERGY",
            "values": Quantity([0.8 * u.TeV, 10.0 * u.TeV]),
            "is_range": True,
        },
    }

    target_position = SkyCoord(ra=229.2, dec=-58.3, unit="deg", frame="icrs")
    region_radius = Angle("0.2 deg")
    region = SphericalCircleSkyRegion(center=target_position, radius=region_radius)
    region_filter = {"type": "sky_region", "opts": {"regions": region}}

    time_filter = Time([53090.12, 53090.13], format="mjd", scale="tt")

    obs_filter = ObservationFilter(
        event_filters=[custom_filter, region_filter], time_filter=time_filter
    )
    events = observation.events
    filtered_events = obs_filter.filter_events(events)

    assert np.all(
        (filtered_events.energy >= 0.8 * u.TeV)
        & (filtered_events.energy < 10.0 * u.TeV)
    )
    assert np.all(
        (filtered_events.time >= time_filter[0])
        & (filtered_events.time < time_filter[1])
    )
    assert np.all(region.center.separation(filtered_events.radec) < region_radius)


@requires_data()
def test_filter_events_parameter(observation):
    events = observation.events
    additional_column = (len(events.table)) * ["AA"]
    additional_column[-1] = "BB"
    additional_column[0] = "BB"
    events.table["CHAR"] = additional_column

    custom_filter = {
        "type": "custom",
        "opts": {"parameter": "CHAR", "values": ["BB"], "is_range": False},
    }
    observation.events = events

    obs_filter = ObservationFilter(event_filters=[custom_filter])
    filtered_events = obs_filter.filter_events(events)

    assert np.all(filtered_events.table["CHAR"] == "BB")
    assert len(filtered_events.table) == 2


@requires_data()
def test_filter_gti(observation):
    time_filter = Time([53090.125, 53090.130], format="mjd", scale="tt")

    obs_filter = ObservationFilter(time_filter=time_filter)

    gti = observation.gti
    filtered_gti = obs_filter.filter_gti(gti)

    assert isinstance(filtered_gti, GTI)
    assert_time_allclose(filtered_gti.time_start, time_filter[0])
    assert_time_allclose(filtered_gti.time_stop, time_filter[1])


@pytest.mark.parametrize(
    "pars",
    [
        {
            "p_in": [
                {"type": "custom", "opts": dict(parameter="PHASE", band=(0.2, 0.8))}
            ],
            "p_out": 0.6,
        },
        {
            "p_in": [
                {
                    "type": "custom",
                    "opts": dict(parameter="ENERGY", band=(0.1, 1) * u.TeV),
                }
            ],
            "p_out": 1,
        },
        {
            "p_in": [],
            "p_out": 1,
        },
    ],
)
def test_check_filter_phase(pars):
    assert_allclose(ObservationFilter._check_filter_phase(pars["p_in"]), pars["p_out"])
