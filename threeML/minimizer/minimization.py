import collections
import math
import time

import numpy
import uncertainties
from iminuit import Minuit

from threeML.io.progress_bar import ProgressBar
from threeML.io.rich_display import display
from threeML.io.table import Table, NumericMatrix
from threeML.parallel.parallel_client import ParallelClient
from threeML.utils.cartesian import cartesian
from threeML.utils.uncertainties_regexpr import get_uncertainty_tokens
from threeML.exceptions.custom_exceptions import custom_warnings

try:

    import ROOT

except ImportError:

    has_ROOT = False

else:

    has_ROOT = True

# Special constants
FIT_FAILED = 1e12


# Define a bunch of custom exceptions relevant for what is being accomplished here

class CannotComputeCovariance(Exception):
    pass


class CannotComputeErrors(Exception):
    pass


class MINOSFailed(Exception):
    pass


class ParameterIsNotFree(Exception):
    pass


class Minimizer(object):
    def __init__(self, function, parameters, ftol=1e-3, verbosity=1):
        """

        :param function: function to be minimized
        :param parameters: ordered dictionary of the FREE parameters in the fit. The order must be the same as
               in the calling sequence of the function to be minimized.
        :param ftol: fractional tolerance to be used in the fit
        :param verbosity: control the verbosity of the output
        :return:
        """

        self.function = function
        self.parameters = parameters
        self.Npar = len(self.parameters.keys())
        self.ftol = ftol
        self.verbosity = verbosity

    def minimize(self):
        raise NotImplemented("This is the method of the base class. Must be implemented by the actual minimizer")


# This is a function to add a method to a class
# We will need it in the MinuitMinimizer

def add_method(self, method, name=None):
    if name is None:
        name = method.func_name

    setattr(self.__class__, name, method)


class MinuitMinimizer(Minimizer):
    # NOTE: this class is built to be able to work both with iMinuit and with a boost interface to SEAL
    # minuit, i.e., it does not rely on functionality that iMinuit provides which is not of the original
    # minuit. This makes the implementation a little bit more cumbersome, but more adaptable if we want
    # to switch back to the SEAL minuit

    def __init__(self, function, parameters, ftol=1e3, verbosity=0):

        super(MinuitMinimizer, self).__init__(function, parameters, ftol, verbosity)

        # Prepare the dictionary for the parameters which will be used by iminuit

        iminuit_init_parameters = {}

        # List of variable names that will be used for iminuit.

        variable_names_for_iminuit = []

        # NOTE: we use the scaled_ versions of value, min_value and max_value because they don't have
        # units, and hence they are much faster to set and retrieve. These are indeed introduced by
        # astromodels to be used for computing-intensive situations like fitting

        for k, par in parameters.iteritems():
            current_name = self._parameter_name_to_minuit_name(k)

            variable_names_for_iminuit.append(current_name)

            # Initial value
            iminuit_init_parameters['%s' % current_name] = par.value

            # Initial delta
            iminuit_init_parameters['error_%s' % current_name] = par.delta

            # Limits
            iminuit_init_parameters['limit_%s' % current_name] = (par.min_value, par.max_value)

            # This is useless, since all parameters here are free,
            # but do it anyway for clarity
            iminuit_init_parameters['fix_%s' % current_name] = False

        # This is to tell Minuit that we are dealing with likelihoods,
        # not chi square
        iminuit_init_parameters['errordef'] = 0.5

        iminuit_init_parameters['print_level'] = verbosity

        # We need to make a function with the parameters as explicit
        # variables in the calling sequence, so that Minuit will be able
        # to probe the parameter's names
        var_spelled_out = ",".join(variable_names_for_iminuit)

        # A dictionary to keep a way to convert from var. name to
        # variable position in the function calling sequence
        # (will use this in contours)

        self.name_to_position = {k: i for i, k in enumerate(variable_names_for_iminuit)}

        # Write and compile the code for such function

        code = 'def _f(self, %s):\n  return self.function(%s)' % (var_spelled_out, var_spelled_out)
        exec code

        # Add the function just created as a method of the class
        # so it will be able to use the 'self' pointer
        add_method(self, _f, "_f")

        # Finally we can instance the Minuit class
        self.minuit = Minuit(self._f, **iminuit_init_parameters)

        self.minuit.tol = ftol  # ftol

        try:

            self.minuit.up = 0.5  # This is a likelihood

        except AttributeError:

            # iMinuit uses errodef, not up

            self.minuit.errordef = 0.5

        self.minuit.strategy = 0  # More accurate

        self._best_fit_parameters = None
        self._function_minimum_value = None

    @property
    def function_minimum_value(self):
        """
        Return the value of the function at the minimum, as found during minimize()

        :return: value of the function at the minimum
        """

        return self._function_minimum_value

    @property
    def best_fit_parameters(self):
        """
        Return a dictionary with the best fit parameters of the last call to .minimize()

        :return: dictionary of best fit parameters
        """
        return self._best_fit_parameters

    @staticmethod
    def _parameter_name_to_minuit_name(parameter):
        """
        Translate the name of the parameter to the format accepted by Minuit

        :param parameter: the parameter name, of the form source.component.shape.parname
        :return: a minuit-friendly name for the parameter, such as source_component_shape_parname
        """

        return parameter.replace(".", "_")

    def _migrad_has_converged(self):

        # In the MINUIT manual this is the condition for MIGRAD to have converged
        # 0.002 * tolerance * UPERROR (which is 0.5 for likelihood)

        return self.minuit.edm <= 0.002 * self.minuit.tol * 0.5

    def _run_migrad(self, trials=10):

        # Repeat Migrad up to trials times, until it converges

        for i in range(trials):

            self.minuit.migrad()

            if self._migrad_has_converged():

                # Converged
                break

            else:

                # Try again
                continue

    def _restore_best_fit(self):
        """
        Set the parameters back to their best fit value

        :return: none
        """

        for k, par in self.parameters.iteritems():

            par.value = self.best_fit_parameters[k]

            minuit_name = self._parameter_name_to_minuit_name(k)

            self.minuit.values[minuit_name] = par.value

    def minimize(self):
        """
        Minimize the function using MIGRAD

        :return: best_fit: a dictionary containing the parameters at their best fit values
                 function_minimum : the value for the function at the minimum

                 NOTE: if the minimization fails, the dictionary will be empty and the function_minimum will be set
                 to minimization.FIT_FAILED
        """

        self._run_migrad(10)

        if not self._migrad_has_converged():

            print("\nMIGRAD did not converge in 10 trials.")

            return collections.OrderedDict(), FIT_FAILED

        else:

            # Make a ordered dict for the results

            self._best_fit_parameters = collections.OrderedDict()

            for k, par in self.parameters.iteritems():

                minuit_name = self._parameter_name_to_minuit_name(k)

                self._best_fit_parameters[k] = self.minuit.values[minuit_name]

            self._function_minimum_value = self.minuit.fval

            # NOTE: hesse must be called AFTER having stored the parameters because it
            # will change the value of the parameters

            self.minuit.hesse()

            # Restore parameters to their best fit after HESS has changed them

            self._restore_best_fit()

            return self._best_fit_parameters, self._function_minimum_value

    def print_fit_results(self):
        """
        Display the results of the last minimization.

        :return: (none)
        """

        # Restore the best fit values, in case something has changed
        self._restore_best_fit()

        # I do not use the print_param facility in iminuit because
        # it does not work well with console output, since it fails
        # to autoprobe that it is actually run in a console and uses
        # the HTML backend instead

        # Create a list of strings to print

        data = []

        # Also store the maximum length to decide the length for the line

        name_length = 0

        for k, v in self.parameters.iteritems():

            minuit_name = self._parameter_name_to_minuit_name(k)

            # Format the value and the error with sensible significant
            # numbers
            x = uncertainties.ufloat(v.value, self.minuit.errors[minuit_name])

            # Add some space around the +/- sign

            rep = x.__str__().replace("+/-", " +/- ")

            data.append([k, rep, v.unit])

            if len(k) > name_length:
                name_length = len(k)

        table = Table(rows=data,
                      names=["Name", "Value", "Unit"],
                      dtype=('S%i' % name_length, str, str))

        display(table)

        print("\nNOTE: errors on parameters are approximate. Use get_errors().\n")

    def print_correlation_matrix(self):
        """
        Display the current correlation matrix
        :return: (none)
        """

        # Print a custom covariance matrix because iminuit does
        # not guess correctly the frontend when 3ML is used
        # from terminal

        cov = self.minuit.covariance

        if cov is None:
            raise CannotComputeCovariance("Cannot compute covariance numerically. This usually means that there are " +
                                          " unconstrained parameters. Fix those or reduce their allowed range, or " +
                                          "use a simpler model.")

        # Get list of parameters

        keys = self.parameters.keys()

        # Convert them to the format for iminuit

        minuit_names = map(lambda k: self._parameter_name_to_minuit_name(k), keys)

        # Accumulate rows and compute the maximum length of the names

        data = []
        length_of_names = 0

        for key1, name1 in zip(keys, minuit_names):

            if len(name1) > length_of_names:
                length_of_names = len(name1)

            this_row = []

            for key2, name2 in zip(keys, minuit_names):
                # Compute correlation between parameter key1 and key2

                corr = cov[(name1, name2)] / (math.sqrt(cov[(name1, name1)]) * math.sqrt(cov[(name2, name2)]))

                this_row.append(corr)

            data.append(this_row)

        # Prepare the dtypes for the matrix

        dtypes = map(lambda x: float, minuit_names)

        # Column names are the parameter names

        cols = keys

        # Finally generate the matrix with the names

        table = NumericMatrix(rows=data,
                              names=cols,
                              dtype=dtypes)

        # Customize the format to avoid too many digits

        for col in table.colnames:
            table[col].format = '2.2f'

        display(table)

    def get_errors(self):
        """
        Compute asymmetric errors using MINOS (slow, but accurate) and print them.

        NOTE: this should be called immediately after the minimize() method

        :return: a dictionary containing the asymmetric errors for each parameter.
        """

        self._restore_best_fit()

        if not self._migrad_has_converged():
            raise CannotComputeErrors("MIGRAD results not valid, cannot compute errors. Did you run the fit first ?")

        try:

            self.minuit.minos()

        except:

            raise

        # except:
        #
        #     raise MINOSFailed("MINOS has failed. This usually means that the fit is very difficult, for example "
        #                       "because of high correlation between parameters. Check the correlation matrix printed"
        #                       "in the fit step, and check contour plots with getContours(). If you are using a "
        #                       "user-defined model, you can also try to "
        #                       "reformulate your model with less correlated parameters.")

        # Make a ordered dict for the results

        errors = collections.OrderedDict()

        for k, par in self.parameters.iteritems():
            minuit_name = self._parameter_name_to_minuit_name(k)

            errors[k] = (self.minuit.merrors[(minuit_name, -1)], self.minuit.merrors[(minuit_name, 1)])

        # Set the parameters back to the best fit value
        self._restore_best_fit()

        # Print a table with the errors

        data = []
        name_length = 0

        for k, v in self.parameters.iteritems():

            # Format the value and the error with sensible significant
            # numbers

            # Process the negative error

            x = uncertainties.ufloat(v.value, abs(errors[k][0]))

            # Split the uncertainty in number, negative error, and exponent (if any)

            num, uncm, exponent = get_uncertainty_tokens(x)

            # Process the positive error

            x = uncertainties.ufloat(v.value, abs(errors[k][1]))

            # Split the uncertainty in number, positive error, and exponent (if any)

            _, uncp, _ = get_uncertainty_tokens(x)

            if exponent is None:

                # Number without exponent

                pretty_string = "%s -%s +%s" % (num, uncm, uncp)

            else:

                # Number with exponent

                pretty_string = "(%s -%s +%s)%s" % (num, uncm, uncp, exponent)

            data.append([k, pretty_string, v.unit])

            if len(k) > name_length:
                name_length = len(k)

        # Create and display the table

        table = Table(rows=data,
                      names=["Name", "Value", "Unit"],
                      dtype=('S%i' % name_length, str, str))

        display(table)

        return errors

    def contours(self, param_1, param_1_minimum, param_1_maximum, param_1_n_steps,
                 param_2=None, param_2_minimum=None, param_2_maximum=None, param_2_n_steps=None,
                 progress=True, **options):
        """
        Generate confidence contours for the given parameters by stepping for the given number of steps between
        the given boundaries. Call it specifying only source_1, param_1, param_1_minimum and param_1_maximum to
        generate the profile of the likelihood for parameter 1. Specify all parameters to obtain instead a 2d
        contour of param_1 vs param_2

        :param param_1: name of the first parameter
        :param param_1_minimum: lower bound for the range for the first parameter
        :param param_1_maximum: upper bound for the range for the first parameter
        :param param_1_n_steps: number of steps for the first parameter
        :param param_2: name of the second parameter
        :param param_2_minimum: lower bound for the range for the second parameter
        :param param_2_maximum: upper bound for the range for the second parameter
        :param param_2_n_steps: number of steps for the second parameter
        :param progress: (True or False) whether to display progress or not
        :param log: by default the steps are taken linearly. With this optional parameter you can provide a tuple of
        booleans which specify whether the steps are to be taken logarithmically. For example,
        'log=(True,False)' specify that the steps for the first parameter are to be taken logarithmically, while they
        are linear for the second parameter. If you are generating the profile for only one parameter, you can specify
         'log=(True,)' or 'log=(False,)' (optional)
        :param: parallel: whether to use or not parallel computation (default:False)
        :return: a : an array corresponding to the steps for the first parameter
                 b : an array corresponding to the steps for the second parameter (or None if stepping only in one
                 direction)
                 contour : a matrix of size param_1_steps x param_2_steps containing the value of the function at the
                 corresponding points in the grid. If param_2_steps is None (only one parameter), then this reduces to
                 an array of size param_1_steps.
        """

        # Figure out if we are making a 1d or a 2d contour

        if param_2 is None:

            n_dimensions = 1

        else:

            n_dimensions = 2

        # Check the options

        p1log = False
        p2log = False
        parallel = False

        if 'log' in options.keys():

            assert len(options['log']) == n_dimensions, ("When specifying the 'log' option you have to provide a " +
                                                         "boolean for each dimension you are stepping on.")

            p1log = bool(options['log'][0])

            if param_2 is not None:
                p2log = bool(options['log'][1])

        if 'parallel' in options.keys():
            parallel = bool(options['parallel'])

        # Generate the steps

        if p1log:

            param_1_steps = numpy.logspace(math.log10(param_1_minimum), math.log10(param_1_maximum),
                                           param_1_n_steps)

        else:

            param_1_steps = numpy.linspace(param_1_minimum, param_1_maximum,
                                           param_1_n_steps)

        if n_dimensions == 2:

            if p2log:

                param_2_steps = numpy.logspace(math.log10(param_2_minimum), math.log10(param_2_maximum),
                                               param_2_n_steps)

            else:

                param_2_steps = numpy.linspace(param_2_minimum, param_2_maximum,
                                               param_2_n_steps)

        else:

            # Only one parameter to step through
            # Put param_2_steps as nan so that the worker can realize that it does not have
            # to step through it

            param_2_steps = numpy.array([numpy.nan])

        # Generate the grid

        grid = cartesian([param_1_steps, param_2_steps])

        # Define the worker which will compute the value of the function at a given point in the grid

        # Restore best fit

        if self._best_fit_parameters:

            self._restore_best_fit()

        else:

            custom_warnings.warn("No best fit to restore before contours computation. "
                                 "Perform the fit before running contours to remove this warnings.")


        # Duplicate the options used for the original minimizer

        new_args = dict(self.minuit.fitarg)

        # Get the minuit names for the parameters

        minuit_param_1 = self._parameter_name_to_minuit_name(param_1)

        if param_2 is None:

            minuit_param_2 = None

        else:

            minuit_param_2 = self._parameter_name_to_minuit_name(param_2)

        # Instance the worker

        contour_worker = ContourWorker(self._f, self.minuit.values, new_args,
                                       minuit_param_1, minuit_param_2,
                                       self.name_to_position)

        # We are finally ready to do the computation

        # Serial and parallel computation are slightly different, so check whether we are in one case
        # or the other

        if not parallel:

            # Serial computation

            if progress:

                # Computation with progress bar

                progress_bar = ProgressBar(grid.shape[0])

                # Define a wrapper which will increase the progress before as well as run the actual computation

                def wrap(args):

                    results = contour_worker(args)

                    progress_bar.increase()

                    return results

                # Do the computation

                results = map(wrap, grid)

            else:

                # Computation without the progress bar

                results = map(contour_worker, grid)

        else:

            # Parallel computation

            # Connect to the engines

            client = ParallelClient(**options)

            # Get a balanced view of the engines

            load_balance_view = client.load_balanced_view()

            # Distribute the work among the engines and start it, but return immediately the control
            # to the main thread

            amr = load_balance_view.map_async(contour_worker, grid)

            # print progress
            n_points = grid.flatten().shape[0]
            progress = ProgressBar(n_points)

            # This loop will check from time to time the status of the computation, which is happening on
            # different threads, and update the progress bar

            while not amr.ready():
                # Check and report the status of the computation every second

                time.sleep(1)

                # if (debug):
                #     stdouts = amr.stdout
                #
                #     # clear_output doesn't do much in terminal environments
                #     for stdout, stderr in zip(amr.stdout, amr.stderr):
                #         if stdout:
                #             print "%s" % (stdout[-1000:])
                #         if stderr:
                #             print "%s" % (stderr[-1000:])
                #     sys.stdout.flush()

                progress.animate(amr.progress - 1)

            # If there have been problems, here is where they will be raised

            results = amr.get()

            # Always display 100% at the end

            progress.animate(n_points)

            # Add a new line after the progress bar
            print("\n")

        # Return results

        return param_1_steps, param_2_steps, numpy.array(results).reshape((param_1_steps.shape[0],
                                                                           param_2_steps.shape[0]))


if has_ROOT:

    class FuncWrapper(ROOT.TPyMultiGenFunction):

        def __init__(self, function, dimensions):

            ROOT.TPyMultiGenFunction.__init__(self, self)
            self.function = function
            self.dimensions = int(dimensions)

        def NDim(self):
            return self.dimensions

        def DoEval(self, args):

            new_args = map(lambda i:args[i],range(self.dimensions))

            return self.function(*new_args)


class ROOTMinimizer(Minimizer):

    def __init__(self, function, parameters, ftol=1e-1, verbosity=10):

        super(ROOTMinimizer, self).__init__(function, parameters, ftol, verbosity)

        # Setup the minimizer algorithm
        self.functor = FuncWrapper(self.function, self.Npar)
        self.minimizer = ROOT.Math.Factory.CreateMinimizer("Minuit", "Minimize")
        self.minimizer.Clear()
        self.minimizer.SetMaxFunctionCalls(1000)
        self.minimizer.SetTolerance(0.1)
        self.minimizer.SetPrintLevel(self.verbosity)
        # self.minimizer.SetStrategy(0)

        self.minimizer.SetFunction(self.functor)

        for i, par in enumerate(self.parameters.values()):

            if par.min_value is not None and par.max_value is not None:

                self.minimizer.SetLimitedVariable(i, par.name, par.value,
                                                  par.delta, par.min_value,
                                                  par.max_value)

            elif par.min_value is not None and par.max_value is None:

                # Lower limited
                self.minimizer.SetLowerLimitedVariable(i, par.name, par.value,
                                                       par.delta, par.min_value)

            elif par.min_value is None and par.max_value is not None:

                # upper limited
                self.minimizer.SetUpperLimitedVariable(i, par.name, par.value,
                                                       par.delta, par.max_value)

            else:

                # No limits
                self.minimizer.SetVariable(i, par.name, par.value, par.delta)

    def minimize(self, minos=False):

        self.minimizer.SetPrintLevel(int(self.verbosity))

        self.minimizer.Minimize()

        # This improves on the error computation
        # self.minimizer.Hesse()

        xs = numpy.array(map(lambda x: x[0], zip(self.minimizer.X(), range(self.Npar))))

        if (minos):

            # Get the errors
            xserr = []
            for i in range(xs.shape[0]):
                minv = ROOT.Double(0)
                maxv = ROOT.Double(0)
                self.minimizer.GetMinosError(i, minv, maxv)
                xserr.append([minv, maxv])
            pass

        else:

            xserr = numpy.array(map(lambda x: x[0], zip(self.minimizer.Errors(), range(self.Npar))))

        #return xs, xserr, self.functor(xs)
        return xs, self.functor(xs)


class ContourWorker(object):
    def __init__(self, function, minuit_values, minuit_args, minuit_param_1, minuit_param_2, name_to_position):

        self._minuit_values = minuit_values

        # Update the values for the parameters with the best fit one

        for key, value in self._minuit_values.iteritems():
            minuit_args[key] = value

        # This is a likelihood
        minuit_args['errordef'] = 0.5

        # Disable printing by iminuit

        minuit_args['print_level'] = 0

        self._minuit_args = minuit_args

        # Store the name of the parameters

        self.minuit_param_1 = minuit_param_1
        self.minuit_param_2 = minuit_param_2

        # Store the function
        self._function = function

        # This is a dictionary which gives the ordinal place for a given parameter.
        # It is used in the corner case where the function has only two parameters,
        # to figure out which is the correct order

        self.name_to_position = name_to_position

    def _create_new_minuit_object(self, args):

        # Now create the new minimizer

        _contour_minuit = Minuit(self._function, **args)

        _contour_minuit.tol = 100

        return _contour_minuit

    def __call__(self, args):

        # Get the values for the parameters
        # If we are stepping in only one direction, value_2 will be nan

        value_1, value_2 = args

        # NOTE: unfortunately iminuit does not allow to change the value of a fixed parameter after
        # the creation of the Minuit class. Hence we need to create a new class each time,
        # which sucks

        # Create a copy of the init args for Minuit

        this_minuit_args = dict(self._minuit_args)

        # Now set the parameters under scrutiny to the current values

        this_minuit_args[self.minuit_param_1] = value_1

        if self.minuit_param_2 is not None:
            this_minuit_args[self.minuit_param_2] = value_2

        # Fix the parameters under scrutiny

        for minuit_name in [self.minuit_param_1, self.minuit_param_2]:

            if minuit_name is None:
                # Only one parameter to analyze

                continue

            if minuit_name not in this_minuit_args.keys():

                raise ParameterIsNotFree("Parameter %s is not a free parameter." % minuit_name)

            else:

                this_minuit_args['fix_%s' % minuit_name] = True

        # Finally create a new minimizer
        this_contour_minuit = self._create_new_minuit_object(this_minuit_args)

        # Handle the corner case where there are no free parameters
        # after fixing the two under scrutiny

        if len(this_contour_minuit.list_of_vary_param()) == 0:

            # All parameters are fixed, just return the likelihood function

            if self.minuit_param_2 is None:

                value = self._function(value_1)

            else:

                # This is needed because the user could specify the
                # variables in a different order than what is specified in the calling sequence
                # of f

                this_variables = [0, 0]
                this_variables[self.name_to_position[self.minuit_param_1]] = value_1
                this_variables[self.name_to_position[self.minuit_param_2]] = value_2

                value = self._function(*this_variables)

            return value

        try:

            this_contour_minuit.migrad()

        # In the following except I cannot catch specific exceptions because I don't exactly know which kind
        # of exception migrad can raise...

        except:

            # In this context this is not such a big deal,
            # because we might be so far from the minimum that
            # the fit cannot converge

            return FIT_FAILED

        return this_contour_minuit.fval
