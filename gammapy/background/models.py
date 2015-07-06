# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Background models.
"""
from __future__ import print_function, division
import numpy as np
from astropy.modeling.models import Gaussian1D
from astropy.units import Quantity
from astropy.coordinates import Angle
from astropy.io import fits
from astropy.table import Table

__all__ = ['GaussianBand2D',
           'CubeBackgroundModel',
           ]

DEFAULT_SPLINE_KWARGS = dict(k=1, s=0)


class GaussianBand2D(object):
    """Gaussian band model.

    This 2-dimensional model is Gaussian in ``y`` for a given ``x``,
    and the Gaussian parameters can vary in ``x``.

    One application of this model is the diffuse emission along the
    Galactic plane, i.e. ``x = GLON`` and ``y = GLAT``.

    Parameters
    ----------
    table : `~astropy.table.Table`
        Table of Gaussian parameters.
        ``x``, ``amplitude``, ``mean``, ``stddev``.
    spline_kwargs : dict
        Keyword arguments passed to `~scipy.interpolate.UnivariateSpline`
    """

    def __init__(self, table, spline_kwargs=DEFAULT_SPLINE_KWARGS):
        self.table = table
        self.parnames = ['amplitude', 'mean', 'stddev']

        from scipy.interpolate import UnivariateSpline
        s = dict()
        for parname in self.parnames:
            x = self.table['x']
            y = self.table[parname]
            s[parname] = UnivariateSpline(x, y, **spline_kwargs)
        self._par_model = s

    def _evaluate_y(self, y, pars):
        """Evaluate Gaussian model at a given ``y`` position.
        """
        return Gaussian1D.evaluate(y, **pars)

    def parvals(self, x):
        """Interpolated parameter values at a given ``x``.
        """
        x = np.asanyarray(x, dtype=float)
        parvals = dict()
        for parname in self.parnames:
            par_model = self._par_model[parname]
            shape = x.shape
            parvals[parname] = par_model(x.flat).reshape(shape)

        return parvals

    def y_model(self, x):
        """Create model at a given ``x`` position.
        """
        x = np.asanyarray(x, dtype=float)
        parvals = self.parvals(x)
        return Gaussian1D(**parvals)

    def evaluate(self, x, y):
        """Evaluate model at a given position ``(x, y)`` position.
        """
        x = np.asanyarray(x, dtype=float)
        y = np.asanyarray(y, dtype=float)
        parvals = self.parvals(x)
        return self._evaluate_y(y, parvals)


def _make_bin_edges_array(lo, hi):
    """Make bin edges array from a low values and a high values array.

    TODO: move this function to somewhere else? (i.e. utils?)

    Parameters
    ----------
    lo : `~numpy.array`
    	lower boundaries
    hi : `~numpy.array`
    	higher boundaries

    Returns
    -------
    bin_edges : `~numpy.array`
    	array of bin edges as [[low], [high]]
    """
    return np.append(lo.flatten(), hi.flatten()[-1:])


class CubeBackgroundModel(object):
    """Cube background model.

    Container class for cube background model (X, Y, energy).
    (X, Y) are detector coordinates (a.k.a. nominal system).
    The class hass methods for reading a model from a fits file,
    write a model to a fits file and plot the models.

    Parameters
    ----------
    detx_bins : `~astropy.coordinates.Angle`
        Spatial bin edges vector (low and high). X coordinate.
    dety_bins : `~astropy.coordinates.Angle`
        Spatial bin edges vector (low and high). Y coordinate.
    energy_bins : `~astropy.units.Quantity`
        Energy bin edges vector (low and high).
    background : `~astropy.units.Quantity`
    	Background cube in (energy, X, Y) format.
    """

    def __init__(self, detx_bins, dety_bins, energy_bins, background):
        self.detx_bins = detx_bins
        self.dety_bins = dety_bins
        self.energy_bins = energy_bins

        self.background = background

    @staticmethod
    def from_fits_bin_table(tbhdu):
        """Read cube background model from binary table in fits file.

        Parameters
        ----------
        tbhdu : `~astropy.io.fits.BinTableHDU`
            HDU binary table for the bg cube

        Returns
        -------
        bg_cube : `~gammapy.models.CubeBackgroundModel`
            bg model cube object
        """
        hdu = tbhdu['BACKGROUND']

        header = hdu.header
        data = hdu.data

        # check correct axis order: 1st X, 2nd Y, 3rd energy, 4th bg
        if (header['TTYPE1'] != 'DETX_LO') or (header['TTYPE2'] != 'DETX_HI'):
            raise ValueError("Expecting X axis in first 2 places, not ({0}, {1})"
                             .format(header['TTYPE1'], header['TTYPE2']))
        if (header['TTYPE3'] != 'DETY_LO') or (header['TTYPE4'] != 'DETY_HI'):
            raise ValueError("Expecting Y axis in second 2 places, not ({0}, {1})"
                             .format(header['TTYPE3'], header['TTYPE4']))
        if (header['TTYPE5'] != 'ENERG_LO') or (header['TTYPE6'] != 'ENERG_HI'):
            raise ValueError("Expecting E axis in third 2 places, not ({0}, {1})"
                             .format(header['TTYPE5'], header['TTYPE6']))
        if (header['TTYPE7'] != 'Bgd'):
            raise ValueError("Expecting bg axis in fourth place, not ({})"
                             .format(header['TTYPE7']))

        # get det X, Y binning
        detx_bins = _make_bin_edges_array(data['DETX_LO'], data['DETX_HI'])
        dety_bins = _make_bin_edges_array(data['DETY_LO'], data['DETY_HI'])
        if header['TUNIT1'] == header['TUNIT2']:
            detx_unit = header['TUNIT1']
        else:
            raise ValueError("Detector X units not matching ({0}, {1})"
                             .format(header['TUNIT1'], header['TUNIT2']))
        if header['TUNIT3'] == header['TUNIT4']:
            dety_unit = header['TUNIT3']
        else:
            raise ValueError("Detector Y units not matching ({0}, {1})"
                             .format(header['TUNIT3'], header['TUNIT4']))
        if not detx_unit == dety_unit:
            ss = "This is odd: detector X and Y units not matching"
            ss += "({0}, {1})".format(detx_unit, dety_unit)
            raise ValueError(ss)
        detx_bins = Angle(detx_bins, detx_unit)
        dety_bins = Angle(dety_bins, dety_unit)

        # get energy binning
        energy_bins = _make_bin_edges_array(data['ENERG_LO'], data['ENERG_HI'])
        if header['TUNIT5'] == header['TUNIT6']:
            energy_unit = header['TUNIT5']
        else:
            raise ValueError("Energy units not matching ({0}, {1})"
                             .format(header['TUNIT5'], header['TUNIT6']))
        energy_bins = Quantity(energy_bins, energy_unit)

        # get background data
        background = data['Bgd'][0]
        background_unit = header['TUNIT7']
        if background_unit in ['1/s/TeV/sr', 's-1 sr-1 TeV-1', '1 / (s sr TeV)']:
            background_unit = '1 / (s TeV sr)'
        elif background_unit in ['1/s/MeV/sr', 'MeV-1 s-1 sr-1', '1 / (s sr MeV)']:
            background_unit = '1 / (s MeV sr)'
        else:
            raise ValueError("Cannot interpret units ({})".format(background_unit))
        background = Quantity(background, background_unit)

        return CubeBackgroundModel(detx_bins=detx_bins,
                                   dety_bins=dety_bins,
                                   energy_bins=energy_bins,
                                   background=background)

    @staticmethod
    def from_fits_image(tbhdu):
        """Read cube background model from image in fits file.

        Parameters
        ----------
        tbhdu : `~astropy.io.fits.BinTableHDU`
            HDU binary table for the bg cube

        Returns
        -------
        bg_cube : `~gammapy.models.CubeBackgroundModel`
            bg model cube object
        """
        raise NotImplementedError


    @staticmethod
    def read_bin_table(filename):
        """Read cube background model from binary table in fits file.

        Parameters
        ----------
        filename : `~string`
            name of file with the bg cube

        Returns
        -------
        bg_cube : `~gammapy.models.CubeBackgroundModel`
            bg model cube object
        """
        hdu = fits.open(filename)
        return CubeBackgroundModel.from_fits_bin_table(hdu)


    @staticmethod
    def read_image(filename):
        """Read cube background model from image in fits file.

        Parameters
        ----------
        filename : `~string`
            name of file with the bg cube

        Returns
        -------
        bg_cube : `~gammapy.models.CubeBackgroundModel`
            bg model cube object
        """
        hdu = fits.open(filename)
        return CubeBackgroundModel.from_fits_image(hdu)        


    def to_astropy_table(self):
        """Convert cube background model to astropy table format.

        Returns
        -------
        name : `~string`
            name of the table
        table : `~astropy..table.Table`
            table containing the bg cube
        """
        # fits unit string
        u_detx = '{0.unit:FITS}'.format(self.detx_bins)
        u_dety = '{0.unit:FITS}'.format(self.dety_bins)
        u_energy = '{0.unit:FITS}'.format(self.energy_bins)
        u_bg = '{0.unit:FITS}'.format(self.background)

        # data arrays
        a_detx_lo = Quantity([self.detx_bins[:-1]])
        a_detx_hi = Quantity([self.detx_bins[1:]])
        a_dety_lo = Quantity([self.dety_bins[:-1]])
        a_dety_hi = Quantity([self.dety_bins[1:]])
        a_energy_lo = Quantity([self.energy_bins[:-1]])
        a_energy_hi = Quantity([self.energy_bins[1:]])
        a_bg = Quantity([self.background])

        # name
        name = 'BACKGROUND'
        # TODO: is it possible to give a name to a `~astropy.table.Table`?
        #       (without writing it in the header)

        # table
        table = Table()
        table['DETX_LO'] = a_detx_lo
        table['DETX_HI'] = a_detx_hi
        table['DETY_LO'] = a_dety_lo
        table['DETY_HI'] = a_dety_hi
        table['ENERG_LO'] = a_energy_lo
        table['ENERG_HI'] = a_energy_hi
        table['Bgd'] = a_bg

        table.meta['E_THRES'] = a_energy_lo.flatten()[0].value

        return name, table


    def to_fits_bin_table(self):
        """Convert cube background model to binary table fits format.

        Returns
        -------
        tbhdu : `~astropy.io.fits.BinTableHDU`
            table containing the bg cube
        """
        # build astropy table
        name, table = self.to_astropy_table()

        data = table.as_array()

        header = fits.Header()
        header.update(table.meta)
 
        tbhdu = fits.BinTableHDU(data, header, name=name)
 
        # Copy over column meta-data
        for colname in table.colnames:
            tbhdu.columns[colname].unit = str(table[colname].unit)

        # TODO: this method works fine but the order of keywords in the table
        # header is not logical: for instnce, list of keywords with column
        # units (TUNITi) is appended after the list of column keywords
        # (TTYPEi, TFORMi), instead of in between.

        return tbhdu


    def to_fits_image(self):
        """Convert cube background model to image fits format.

        Returns
        -------
        hdu : `~astropy.io.fits.ImageHDU`
            image containing the bg cube
        """
        hdu = fits.ImageHDU(data=self.background.value)
        #TODO: store units (of bg) somewhere in header??!!!!
        #TODO: implement WCS object to be able to read the det coords
        #TODO: energy binning: store in HDU table like for SpectralCube class

        return hdu


    def write_bin_table(self, *args, **kwargs):
        """Write cube background model to binary table in fits file.

        This function is expected to be called on a
        `~astropy.io.fits.BinTableHDU` object.
        It calls `~astropy.io.fits.BinTableHDU.writeto`,
        forwarding all arguments.
        """
        self.to_fits_bin_table().writeto(*args, **kwargs)


    def write_image(self, *args, **kwargs):
        """Write cube background model to image in fits file.

        This function is expected to be called on a
        `~astropy.io.fits.ImageHDU` object.
        It calls `~astropy.io.fits.ImageHDU.writeto`,
        forwarding all arguments.
        """
        self.to_fits_image().writeto(*args, **kwargs)


    @property
    def image_extent(self):
        """Image extent `(x_lo, x_hi, y_lo, y_hi)`.

        Returns
        -------
        im_extent : `~astropy.coordinates.Angle`
            array of bins with the image extent
        """
        bx = self.detx_bins.degree
        by = self.dety_bins.degree
        return Angle([bx[0], bx[-1], by[0], by[-1]], 'degree')


    @property
    def spectrum_extent(self):
        """Spectrum extent `(e_lo, e_hi)`.

        Returns
        -------
        spec_extent : `~astropy.units.Quantity`
            array of bins with the spectrum extent
        """
        b = self.energy_bins.to('TeV')
        return Quantity([b[0], b[-1]], 'TeV')


    @property
    def image_bin_centers(self):
        """Image bin centers `(x, y)`.

        Returns
        -------
        det_edges_centers : `~astropy.coordinates.Angle`
            array of bins with the image bin centers
        """
        detx_edges_low = self.detx_bins[:-1]
        detx_edges_high = self.detx_bins[1:]
        detx_edges_centers = (detx_edges_low + detx_edges_high)/2.
        dety_edges_low = self.dety_bins[:-1]
        dety_edges_high = self.dety_bins[1:]
        dety_edges_centers = (dety_edges_low + dety_edges_high)/2.
        return Angle([detx_edges_centers, dety_edges_centers])


    @property
    def spectrum_bin_centers(self):
        """Spectrum bin centers (log center)

        Returns
        -------
        energy_bin_centers : `~astropy.units.Quantity`
            array of bins with the spectrum bin centers
        """
        energy_edges_low = self.energy_bins[:-1]
        energy_edges_high = self.energy_bins[1:]
        energy_bin_centers = 10.**(0.5*(np.log10(energy_edges_low.to('TeV').value*energy_edges_high.to('TeV').value)))
        return Quantity(energy_bin_centers, 'TeV')


    def find_det_bin(self, det):
        """Find the bin that contains the specified det (X, Y) pair.

        TODO: implement test as suggested in:
            https://github.com/gammapy/gammapy/pull/292#discussion_r33843508
        Parameters
        ----------
        det : `~astropy.coordinates.Angle`
            det (X, Y) pair to search for

        Returns
        -------
        bin_pos : `~int`
            index of the det bin containing the specified det (X, Y) pair
        bin_edges : `~astropy.units.Quantity`
            det bin edges (x_lo, x_hi, y_lo, y_hi)
        """
        # check shape of det: only 1 pair is accepted
        nvalues = len(det.flatten())
        if nvalues != 2:
            print("Expected exactly 2 values for det (X, Y), got {}.".format(nvalues))
            raise IndexError

        # check that the specified det is within the boundaries of the model
        det_extent = self.image_extent
        if not ((det_extent[0] <= det[0]) and (det[0] < det_extent[1])) or not ((det_extent[2] <= det[1]) and (det[1] < det_extent[3])):
            print("Specified det {0} is outside the boundaries {1}.".format(det, det_extent))
            raise ValueError

        detx_edges_low = self.detx_bins[:-1]
        detx_edges_high = self.detx_bins[1:]
        dety_edges_low = self.dety_bins[:-1]
        dety_edges_high = self.dety_bins[1:]
	bin_pos_x = np.searchsorted(detx_edges_high, det[0])
	bin_pos_y = np.searchsorted(dety_edges_high, det[1])
        bin_pos = np.array([bin_pos_x, bin_pos_y])
        bin_edges = Angle([detx_edges_low[bin_pos[0]], detx_edges_high[bin_pos[0]],
                           dety_edges_low[bin_pos[1]], dety_edges_high[bin_pos[1]]])

        return bin_pos, bin_edges


    def find_energy_bin(self, energy):
        """Find the bin that contains the specified energy value.

        TODO: implement test as suggested in:
            https://github.com/gammapy/gammapy/pull/292#discussion_r33843508
        Parameters
        ----------
        energy : `~astropy.units.Quantity`
            energy to search for

        Returns
        -------
        bin_pos : `~int`
            index of the energy bin containing the specified energy
        bin_edges : `~astropy.units.Quantity`
            energy bin edges [E_min, E_max)
        """
        # check shape of energy: only 1 value is accepted
        nvalues = len(energy.flatten())
        if nvalues != 1:
            print("Expected exactly 1 value for energy, got {}.".format(nvalues))
            raise IndexError

        # check that the specified energy is within the boundaries of the model
        energy_extent = self.spectrum_extent
        if not (energy_extent[0] <= energy) and (energy < energy_extent[1]):
            print("Specified energy {0} is outside the boundaries {1}.".format(energy, energy_extent))
            raise ValueError

        energy_edges_low = self.energy_bins[:-1]
        energy_edges_high = self.energy_bins[1:]
	bin_pos = np.searchsorted(energy_edges_high, energy)
        bin_edges = Quantity([energy_edges_low[bin_pos], energy_edges_high[bin_pos]])

        return bin_pos, bin_edges


    def plot_images(self, energy=None):
        """Plot images for each energy bin.

        Save images in files: several canvases with a few images
        each, 1 file per canvas.
        If specifying a particular energy, the function returns the
        figure of the specific energy bin containing the specified
        value. If no energy is specified, no figure is returned,
        since it would be very memory consuming.

        Parameters
        ----------
        energy : `~astropy.units.Quantity`, optional
        	energy of bin to plot the bg model
      	
        Returns
        -------
        fig : `~matplotlib.figure.Figure`
            figure with image of bin of the bg model for the
            selected energy value (if any), optional
        axes : `~matplotlib.pyplot.axes`
            axes of the figure, optional
        image : !!!!!!!!!!!!
            !!!!!!!!!!!!!!!!, optional(??!!)
        """
        import matplotlib.pyplot as plt
        from matplotlib.colors import LogNorm
        from mpl_toolkits.axes_grid1 import make_axes_locatable

        do_only_1_plot = False # general case: print all plots
        if energy:
            energy = energy.flatten() # flatten
            # check shape of energy: only 1 value is accepted
            nvalues = len(energy)
            if nvalues != 1:
                print("Expected exactly 1 value for energy, got {}.".format(nvalues))
                raise IndexError
            else:
                energy = Quantity(energy[0])
                print("Reqested plot only for 1 energy: {}".format(energy))
                do_only_1_plot = True

        n_energy_bins = len(self.energy_bins) - 1
        nimages = n_energy_bins
        ncols = 4
        nrows = 4
        if do_only_1_plot:
            n_energy_bins = nimages = ncols = nrows = 1
        npads_per_canvas = ncols*nrows

        fig, axes = plt.subplots(nrows=nrows, ncols=ncols)
        fig.set_size_inches(35., 35., forward=True)
        count_images = 1
        count_canvases = 1
        count_pads = 1

        extent = self.image_extent
        energy_bin_centers = self.spectrum_bin_centers
        if do_only_1_plot:
            # find energy bin containing the specified energy
            energy_bin, energy_bin_edges = self.find_energy_bin(energy)
            ss_energy_bin_edges = "[{0}, {1}) {2}".format(energy_bin_edges[0].value, energy_bin_edges[1].value, energy_bin_edges.unit)
            print("Found energy {0} in bin {1} with boundaries {2}.".format(energy, energy_bin, ss_energy_bin_edges))

        for ii in range(n_energy_bins):
            if do_only_1_plot:
                ii = energy_bin
            print("ii", ii)
            data = self.background[ii]
            energy_bin_center = energy_bin_centers[ii]
            print ( "  image({}) canvas({}) pad({})".format(count_images, count_canvases, count_pads))

            if do_only_1_plot:
                fig.set_size_inches(8., 8., forward=True)
                ax = axes
            else:
                ax = axes.flat[count_pads - 1]
            image = ax.imshow(data.value, extent=extent.value, interpolation='nearest',
                              norm=LogNorm(), cmap='afmhot') # color log scale
            if do_only_1_plot:
                ax.set_title('Energy = [{0:.1f}, {1:.1f}) {2}'.format(energy_bin_edges[0].value, energy_bin_edges[1].value, energy_bin_edges.unit))
            else:
                ax.set_title('Energy = {:.1f}'.format(energy_bin_center))
            ax.set_xlabel('X / {}'.format(extent.unit))
            ax.set_ylabel('Y / {}'.format(extent.unit))
            divider = make_axes_locatable(ax)
            cax = divider.append_axes("right", size="5%", pad=0.05)
            fig.colorbar(image, cax=cax, label='Bg rate / {}'.format(data.unit))

            count_pads += 1 # increase

            if count_pads > npads_per_canvas or count_images == nimages:
                print("Canvas full, saving and creating a new canvas")
                if do_only_1_plot:
                    filename = "cube_background_model_image{0}{1}.png".format(energy.value, energy.unit)
                else:
                    filename = "cube_background_model_images{}.png".format(count_canvases)
                print('Writing {}'.format(filename))
                fig.savefig(filename)
                if not do_only_1_plot:
                    plt.close('all') # close all open figures
                    fig, axes = plt.subplots(nrows=nrows, ncols=ncols)
                    fig.set_size_inches(35., 35., forward=True)
                count_canvases += 1 # increase
                count_pads = 1 # reset

            count_images += 1 # increase

        if do_only_1_plot:
            return fig, axes, image


    def plot_spectra(self, det=None):
        """Plot spectra for each spatial (X, Y) bin.

        Save images in files: several canvases with a few images
        each, 1 file per canvas.
        If specifying a particular det (X,Y) pair, the function
        returns the figure of the specific det bin containing the
        specified value. If no det is specified, no figure is
        returned, since it would be very memory consuming.

        Parameters
        ----------
        det : `~astropy.units.Quantity`, optional
            det (X,Y) pair of bin to plot the bg model
      	
        Returns
        -------
        fig : `~matplotlib.figure.Figure`
            figure with image of bin of the bg model for the
            selected det (X,Y) pair (if any), optional
        axes : `~matplotlib.pyplot.axes`
            axes of the figure, optional
        image : !!!!!!!!!!!!
            !!!!!!!!!!!!!!!!, optional(??!!)
        """
        import matplotlib.pyplot as plt

        do_only_1_plot = False # general case: print all plots
        if det:
            det = det.flatten() # flatten
            # check shape of det: only 1 pair is accepted
            nvalues = len(det.flatten())
            if nvalues != 2:
                print("Expected exactly 2 values for det (X, Y), got {}.".format(nvalues))
                raise IndexError
            else:
                print("Reqested plot only for 1 det: {}".format(det))
                do_only_1_plot = True

        n_det_bins_x = len(self.detx_bins) - 1
        n_det_bins_y = len(self.dety_bins) - 1
        nimages = n_det_bins_x*n_det_bins_y
        ncols = 4
        nrows = 4
        if do_only_1_plot:
            n_det_bins = n_det_bins_x = n_det_bins_y = nimages = ncols = nrows = 1
        npads_per_canvas = ncols*nrows

        fig, axes = plt.subplots(nrows=nrows, ncols=ncols)
        fig.set_size_inches(25., 25., forward=True)
        count_images = 1
        count_canvases = 1
        count_pads = 1

        energy_points = self.spectrum_bin_centers
        det_bin_centers = self.image_bin_centers
        if do_only_1_plot:
            # find det bin containing the specified det coordinates
            det_bin, det_bin_edges = self.find_det_bin(det)
            ss_detx_bin_edges = "[{0}, {1}) {2}".format(det_bin_edges[0].value, det_bin_edges[1].value, det_bin_edges.unit)
            ss_dety_bin_edges = "[{0}, {1}) {2}".format(det_bin_edges[2].value, det_bin_edges[3].value, det_bin_edges.unit)
            print("Found det {0} in bin {1} with boundaries {2}, {3}.".format(det, det_bin, ss_detx_bin_edges, ss_dety_bin_edges))

        for ii in range(n_det_bins_x):
            if do_only_1_plot:
                ii = det_bin[0]
            print("ii", ii)
            for jj in range(n_det_bins_y):
                if do_only_1_plot:
                    jj = det_bin[1]
                print(" jj", jj)
                data = self.background[:, ii, jj]
                detx_bin_center = det_bin_centers[0, ii]
                dety_bin_center = det_bin_centers[0, jj]
                print ( "  image({}) canvas({}) pad({})".format(count_images, count_canvases, count_pads))

                if do_only_1_plot:
                    fig.set_size_inches(8., 8., forward=True)
                    ax = axes
                else:
                    ax = axes.flat[count_pads - 1]
                image = ax.plot(energy_points.to('TeV'), data, drawstyle='default') # connect points with lines
                ax.loglog() # double log scale # slow!
                if do_only_1_plot:
                    ss_detx_bin_edges = "[{0:.1f}, {1:.1f}) {2}".format(det_bin_edges[0].value, det_bin_edges[1].value, det_bin_edges.unit)
                    ss_dety_bin_edges = "[{0:.1f}, {1:.1f}) {2}".format(det_bin_edges[2].value, det_bin_edges[3].value, det_bin_edges.unit)

                    ax.set_title('Det = {0} {1}'.format(ss_detx_bin_edges, ss_dety_bin_edges))
                else:
                    ss_det_bin_center = "({0:.1f}, {1:.1f})".format(detx_bin_center, dety_bin_center)
                    ax.set_title('Det = {}'.format(ss_det_bin_center))
                ax.set_xlabel('E / {}'.format(energy_points.unit))
                ax.set_ylabel('Bg rate / {}'.format(data.unit))
                count_pads += 1 # increase

                if count_pads > npads_per_canvas or count_images == nimages:
                    print("Canvas full, saving and creating a new canvas")
                    if do_only_1_plot:
                        filename = "cube_background_model_spectrum{0}{2}{1}{2}.png".format(det.value[0], det.value[1], det.unit)
                    else:
                        filename = "cube_background_model_spectra{}.png".format(count_canvases)
                    print('Writing {}'.format(filename))
                    fig.savefig(filename)
                    if not do_only_1_plot:
                        plt.close('all') # close all open figures
                        fig, axes = plt.subplots(nrows=nrows, ncols=ncols)
                        fig.set_size_inches(25., 25., forward=True)
                    count_canvases += 1 # increase
                    count_pads = 1 # reset

                count_images += 1 # increase

        if do_only_1_plot:
            return fig, axes, image
