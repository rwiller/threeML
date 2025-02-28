import abc
import collections
import math

import numpy as np
from future.utils import with_metaclass

try:

    # see if we have mpi and/or are using parallel

    from mpi4py import MPI

    if MPI.COMM_WORLD.Get_size() > 1:  # need parallel capabilities
        using_mpi = True

        comm = MPI.COMM_WORLD
        rank = comm.Get_rank()

    else:

        using_mpi = False
except:

    using_mpi = False


from astromodels.core.model import Model
from astromodels.functions.function import ModelAssertionViolation

from threeML.analysis_results import BayesianResults
from threeML.data_list import DataList
from threeML.exceptions.custom_exceptions import (LikelihoodIsInfinite,
                                                  custom_warnings)
from threeML.io.logging import setup_logger
from threeML.plugins.DispersionSpectrumLike import DispersionSpectrumLike
from threeML.plugins.SpectrumLike import SpectrumLike
from threeML.utils.numba_utils import nb_sum
from threeML.utils.statistics.stats_tools import aic, bic, dic
from threeML.utils.spectrum.share_spectrum import ShareSpectrum

log = setup_logger(__name__)


class SamplerBase(with_metaclass(abc.ABCMeta, object)):
    def __init__(self, likelihood_model: Model, data_list: DataList, **kwargs):
        """

        The base class for all bayesian samplers. Provides a common interface
        to access samples and byproducts of fits.


        :param likelihood_model: the likelihood model
        :param data_list: the data list
        :param share_spectrum: (optional) Should the spectrum be shared between detectors
        with the same input energy bins?
        :returns:
        :rtype:

        """

        self._samples = None
        self._raw_samples = None
        self._sampler = None
        self._log_like_values = None
        self._log_probability_values = None
        self._results = None
        self._is_setup = False
        self._is_registered = False
        self._likelihood_model = likelihood_model
        self._data_list = data_list

        self._n_plugins = len(list(self._data_list.keys()))

        # Share spectrum flag if the spectrum should only be calculated
        # once when different data_list entries have the same input energy bins.
        # Can speed up the fits a lot if many similar detectors are used.
        if "share_spectrum" in kwargs:
            self._share_spectrum = kwargs["share_spectrum"]
            assert (
                type(self._share_spectrum) == bool
            ), "share_spectrum must be False or True."
            if self._share_spectrum:
                self._share_spectrum_object = ShareSpectrum(self._data_list)
                log.debug("Share spectrum has been initalized")
        else:
            self._share_spectrum = False

    @abc.abstractmethod
    def setup(self):
        pass

    @abc.abstractmethod
    def sample(self):
        pass

    @property
    def results(self):

        return self._results

    @property
    def samples(self):
        """
        Access the samples from the posterior distribution generated by the selected sampler

        :return: a dictionary with the samples from the posterior distribution for each parameter
        """
        return self._samples

    @property
    def raw_samples(self):
        """
        Access the samples from the posterior distribution generated by the selected sampler in raw form (i.e.,
        in the format returned by the sampler)

        :return: the samples as returned by the sampler
        """

        return self._raw_samples

    @property
    def log_like_values(self):
        """
        Returns the value of the log_likelihood found by the bayesian sampler while samplin  g from the posterior. If
        you need to find the values of the parameters which generated a given value of the log. likelihood, remember
        that the samples accessible through the property .raw_samples are ordered in the same way as the vector
        returned by this method.

        :return: a vector of log. like values
        """
        return self._log_like_values

    @property
    def log_probability_values(self):
        """
        Returns the value of the log_probability (posterior) found by the bayesian sampler while sampling from the posterior. If
        you need to find the values of the parameters which generated a given value of the log. likelihood, remember
        that the samples accessible through the property .raw_samples are ordered in the same way as the vector
        returned by this method.

        :return: a vector of log probabilty values
        """

        return self._log_probability_values

    @property
    def log_marginal_likelihood(self):
        """
        Return the log marginal likelihood (evidence) if computed
        :return:
        """

        return self._marginal_likelihood

    def restore_median_fit(self):
        """
        Sets the model parameters to the mean of the marginal distributions
        """
        idx = self._log_probability_values.argmax()
        for i, (parameter_name, parameter) in enumerate(self._free_parameters.items()):

            par = self._samples[parameter_name][idx]

            parameter.value = par

    def _build_samples_dictionary(self):
        """
        Build the dictionary to access easily the samples by parameter

        :return: none
        """

        self._samples = collections.OrderedDict()

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.items()):
            # Add the samples for this parameter for this source

            self._samples[parameter_name] = self._raw_samples[:, i]

    def _build_results(self):
        """
        build the results after a fit is performed

        :returns:
        :rtype:

        """

        # set the median fit

        self.restore_median_fit()
        
        # Find maximum of the log posterior
        idx = self._log_probability_values.argmax()

        # Get parameter values at the maximum
        approximate_MAP_point = self._raw_samples[idx, :]

        # Sets the values of the parameters to their MAP values
        for i, parameter in enumerate(self._free_parameters):

            self._free_parameters[parameter].value = approximate_MAP_point[i]

        # Get the value of the posterior for each dataset at the MAP
        log_posteriors = collections.OrderedDict()

        log_prior = self._log_prior(approximate_MAP_point)

        # keep track of the total number of data points
        # and the total posterior

        total_n_data_points = 0

        total_log_posterior = 0

        for dataset in list(self._data_list.values()):

            log_posterior = dataset.get_log_like() + log_prior

            log_posteriors[dataset.name] = log_posterior

            total_n_data_points += dataset.get_number_of_data_points()

            total_log_posterior += log_posterior

        # compute the statistical measures

        statistical_measures = collections.OrderedDict()

        # compute the point estimates

        statistical_measures["AIC"] = aic(
            total_log_posterior, len(
                self._free_parameters), total_n_data_points
        )
        statistical_measures["BIC"] = bic(
            total_log_posterior, len(
                self._free_parameters), total_n_data_points
        )

        this_dic, pdic = dic(self)

        # compute the posterior estimates

        statistical_measures["DIC"] = this_dic
        statistical_measures["PDIC"] = pdic

        if self._marginal_likelihood is not None:

            statistical_measures["log(Z)"] = self._marginal_likelihood

        # TODO: add WAIC

        # Instance the result

        self._results = BayesianResults(
            self._likelihood_model,
            self._raw_samples,
            log_posteriors,
            statistical_measures=statistical_measures,
            log_probabilty=self._log_like_values
        )

    def _update_free_parameters(self):
        """
        Update the dictionary of the current free parameters
        :return:
        """

        self._free_parameters = self._likelihood_model.free_parameters

    def get_posterior(self, trial_values):
        """Compute the posterior for the normal sampler"""

        # Assign this trial values to the parameters and
        # store the corresponding values for the priors

        # self._update_free_parameters()

        assert len(self._free_parameters) == len(trial_values), (
            "Something is wrong. Number of free parameters "
            "do not match the number of trial values."
        )

        log_prior = 0

        # with use_

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.items()):

            prior_value = parameter.prior(trial_values[i])

            if prior_value == 0:
                # Outside allowed region of parameter space

                return -np.inf

            else:

                parameter.value = trial_values[i]

                log_prior += math.log10(prior_value)

        log_like = self._log_like(trial_values)

        # print("Log like is %s, log_prior is %s, for trial values %s" % (log_like, log_prior,trial_values))

        return log_like + log_prior

    def _log_prior(self, trial_values):
        """Compute the sum of log-priors, used in the parallel tempering sampling"""

        # Compute the sum of the log-priors

        log_prior = 0

        for i, (parameter_name, parameter) in enumerate(self._free_parameters.items()):

            prior_value = parameter.prior(trial_values[i])

            if prior_value == 0:
                # Outside allowed region of parameter space

                return -np.inf

            else:

                parameter.value = trial_values[i]

                log_prior += math.log10(prior_value)

        return log_prior

    def _log_like(self, trial_values):
        """Compute the log-likelihood"""

        # Get the value of the log-likelihood for this parameters

        try:

            log_like_values = np.zeros(self._n_plugins)

            # Loop over each dataset and get the likelihood values for each set
            if not self._share_spectrum:
                # Old way; every dataset independendly - This is fine if the
                # spectrum calc is fast.

                for i, dataset in enumerate(self._data_list.values()):

                    log_like_values[i] = dataset.get_log_like()

            else:
                # If the calculation for the input spectrum of one of the sources is expensive
                # we want to avoid calculating the same thing several times.

                # Precalc the spectrum for all different Ebin_in that are used in the plugins
                precalc_fluxes = []

                for base_key, e_edges in zip(self._share_spectrum_object.base_plugin_key,
                                            self._share_spectrum_object.data_ein_edges):
                    if e_edges is None:
                        precalc_fluxes.append(None)
                    else:
                        precalc_fluxes.append(
                            self._data_list[base_key]._integral_flux()
                        )

                # Use these precalculated spectra to get the log_like for all plugins
                for i, dataset in enumerate(list(self._data_list.values())):
                    # call get log_like with precalculated spectrum
                    if self._share_spectrum_object.data_ein_edges[
                            self._share_spectrum_object.data_ebin_connect[i]] is not None:
                        log_like_values[i] = dataset.get_log_like(
                            precalc_fluxes=precalc_fluxes[
                                self._share_spectrum_object.data_ebin_connect[i]
                            ]
                        )
                    else:
                        log_like_values[i] = dataset.get_log_like()

        except ModelAssertionViolation:

            # Fit engine or sampler outside of allowed zone

            return -np.inf

        except:

            # We don't want to catch more serious issues

            raise

        # Sum the values of the log-like

        log_like = nb_sum(log_like_values)

        if not np.isfinite(log_like):
            # Issue warning
            keys = self._likelihood_model.free_parameters.keys()
            params = [
                f"{key}: {self._likelihood_model.free_parameters[key].value}"
                for key in keys
            ]

            log.warning(
                f"Likelihood value is infinite for parameters: {params}"
            )

            return -np.inf

        return log_like


class MCMCSampler(SamplerBase):
    def __init__(self, likelihood_model, data_list, **kwargs):

        super(MCMCSampler, self).__init__(
            likelihood_model, data_list, **kwargs)

    def _get_starting_points(self, n_walkers, variance=0.1):

        # Generate the starting points for the walkers by getting random
        # values for the parameters close to the current value

        # Fractional variance for randomization

        # (0.1 means var = 0.1 * value )
        p0 = []

        for i in range(n_walkers):
            this_p0 = [
                x.get_randomized_value(variance)
                for x in list(self._free_parameters.values())
            ]

            p0.append(this_p0)

        return p0


class UnitCubeSampler(SamplerBase):
    def __init__(self, likelihood_model, data_list, **kwargs):

        super(UnitCubeSampler, self).__init__(
            likelihood_model, data_list, **kwargs)

    def _construct_unitcube_posterior(self, return_copy=False):
        """

        Here, we construct the prior and log. likelihood for multinest etc on the unit cube
        """

        # First update the free parameters (in case the user changed them after the construction of the class)
        self._update_free_parameters()

        def loglike(trial_values, ndim=None, params=None):

            # NOTE: the _log_like function DOES NOT assign trial_values to the parameters

            for i, parameter in enumerate(self._free_parameters.values()):
                parameter.value = trial_values[i]

            log_like = self._log_like(trial_values)

            # if self._verbose:
            #     n_par = len(self._free_parameters)

            #     print(
            #         "Trial values %s gave a log_like of %s"
            #         % (["%.2g" % trial_values[i] for i in range(n_par)], log_like)
            #     )

            return log_like

        # Now construct the prior
        # MULTINEST priors are defined on the unit cube
        # and should return the value in the bounds... not the
        # probability. Therefore, we must make some transforms

        if return_copy:

            def prior(cube):
                params = cube.copy()

                for i, (parameter_name, parameter) in enumerate(
                    self._free_parameters.items()
                ):

                    try:

                        params[i] = parameter.prior.from_unit_cube(params[i])

                    except AttributeError:

                        raise RuntimeError(
                            "The prior you are trying to use for parameter %s is "
                            "not compatible with sampling from a unitcube"
                            % parameter_name
                        )
                return params

        else:

            def prior(params, ndim=None, nparams=None):

                for i, (parameter_name, parameter) in enumerate(
                    self._free_parameters.items()
                ):

                    try:

                        params[i] = parameter.prior.from_unit_cube(params[i])

                    except AttributeError:

                        raise RuntimeError(
                            "The prior you are trying to use for parameter %s is "
                            "not compatible with sampling from a unitcube"
                            % parameter_name
                        )

            # Give a test run to the prior to check that it is working. If it crashes while multinest is going
            # it will not stop multinest from running and generate thousands of exceptions (argh!)
            n_dim = len(self._free_parameters)

            _ = prior([0.5] * n_dim, n_dim, [])

        return loglike, prior
