# Copyright (C) 2016, Adam Gleave
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import filecmp, functools, itertools, tempfile, os, select, subprocess, traceback
import celery
from redis import Redis
import memoize.redis

from benchmark.celery import app
from benchmark.mode import CompressionMode
import benchmark.config as config

import numpy as np
from scipy import optimize

db = Redis(host=config.REDIS_HOST, port=config.REDIS_PORT)
store = memoize.redis.wrap(db)
memo = memoize.Memoizer(store)

def corpus_path(input_fname):
  if os.path.isabs(input_fname):
    raise RuntimeError("Input paths must be relative, but '{0}' absolute".format(input_fname))
  else:
    return os.path.join(config.CORPUS_DIR, input_fname)

def compressed_filesize(compressor, input_fname, paranoia):
  def timeout(process, callback):
    for (timeout, complaint) in config.timeouts():
      try:
        return_code = process.wait(timeout)
        if return_code != 0:
          return "ERROR: compressor returned status {0}".format(return_code)
        else:
          return callback()
      except subprocess.TimeoutExpired:
        print(complaint)
        # timed out
    p.kill()
    return float('inf')

  with tempfile.NamedTemporaryFile(prefix='compression_en') as compressed:
    input_fname = corpus_path(input_fname)
    p = compressor(input_fname, compressed.name, CompressionMode.compress)
    def compressed_callback():
      if paranoia:
        with tempfile.NamedTemporaryFile(prefix='compression_de') as decompressed:
          p = compressor(compressed.name, decompressed.name, CompressionMode.decompress)
          def decompressed_callback():
            if not filecmp.cmp(input_fname, decompressed.name):
              return "ERROR: decompressed file differs from original"
            else:
              return os.path.getsize(compressed.name)
          return timeout(p, decompressed_callback)
      else:
        return os.path.getsize(compressed.name)
    return timeout(p, compressed_callback)


@app.task
@memo
def ext_compressor(fname, paranoia, name):
  compressor = config.EXT_COMPRESSORS[name]
  return compressed_filesize(compressor, fname, paranoia)

def my_compressor_start_args(classname):
  classpath = config.find_sbt_classpath() + ':' + config.BIN_DIR
  class_qualified = 'uk.ac.cam.cl.arg58.mphil.compression.' + classname
  return ['java', '-Xms512M', '-Xmx1536M',
          '-classpath', classpath, class_qualified]

def my_compressor_end_args(base, algorithms):
  args = ['--base', base]
  if algorithms:
    args += ['--model'] + algorithms
  return args

def build_my_compressor(base, algorithms=None):
  def run_compressor(in_file, out_file, mode):
    starting_args = my_compressor_start_args('Compressor')
    ending_args = my_compressor_end_args(base, algorithms)
    compressor = config.build_compressor(starting_args,
                                         ['compress'] + ending_args,
                                         ['decompress'] + ending_args)
    return compressor(in_file, out_file, mode)
  return run_compressor

def run_multicompressor():
  args = my_compressor_start_args('MultiCompressor')
  # use line buffer
  return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, bufsize=1)

multi_compressor = None
#SOMEDAY: if result is in cache, quicker to hit DB locally rather than farming it out via Celery.
@app.task
@memo
def my_compressor(fname, paranoia, base, algorithms):
  try:
    def error_str(suffix):
      return 'multi_compressor: {0} with {1} (paranoia={2}) on {3}: {4}'\
             .format(base, algorithms, paranoia, fname, suffix)
    if paranoia:
      # Slow but makes sure the results are valid.
      # Uses the standard Compressor interface to compress the file then decompress it,
      # and verifies the decompressed file is the same as the original
      compressor = build_my_compressor(base, algorithms)
      return compressed_filesize(compressor, fname, paranoia)
    else:
      # Uses the MultiCompressor interface with the measure command.
      # Runs commands in the same JVM, one after the other, and doesn't write any files.
      # Faster, but doesn't perform any verification checks.
      global multi_compressor
      if not multi_compressor:
        multi_compressor = run_multicompressor()

      cmd =  'measure {0} '.format(corpus_path(fname))
      cmd += ' '.join(my_compressor_end_args(base, algorithms)) + '\n'
      try:
        multi_compressor.stdin.write(cmd)
      except BrokenPipeError:
        print("WARNING: MultiCompressor has quit, restarting.")
        multi_compressor.kill() # make sure it's dead
        multi_compressor = run_multicompressor()
        # if this still fails, propagate the exception (only retry once)
        multi_compressor.stdin.write(cmd)

      for (timeout, complaint) in config.timeouts():
        ready_read, _, _ = select.select([multi_compressor.stdout, multi_compressor.stderr], [], [],
                                         timeout)
        if not ready_read:
          print(error_str(complaint))
        if multi_compressor.stderr in ready_read:
          for line in multi_compressor.stderr:
            print(error_str("received error from MultiCompressor: " + line.strip()))
        if multi_compressor.stdout in ready_read:
          out = multi_compressor.stdout.readline().strip()
          prefix = 'BITS WRITTEN: '
          if out.find(prefix) != 0:
            raise RuntimeError(error_str("unexpected output: '" + out + "'"))
          bits = int(out[len(prefix):])
          return bits / 8 # compressed filesize in bytes
      multi_compressor.kill()
      multi_compressor = None
      return float('inf')
  except Exception:
    print(error_str("error executing task: " + traceback.format_exc()))
    return float('inf')

def corpus_size(fname):
  return os.path.getsize(corpus_path(fname))

def mean_effectiveness(compressed_sizes, original_sizes):
  n = len(compressed_sizes)
  assert(n == len(original_sizes))

  sum = 0
  for c, o in zip(compressed_sizes, original_sizes):
    sum += c / o * 8
  return sum / n

# This function doesn't need to be memoized, as it's a function of memoized values.
# However, optimisation is itself expensive, and the cached result is fairly small.
# So may as well save it.
@memo
def ppm_minimize(fnames, paranoia, prior, depth, initial_guess, method='Nelder-Mead'):
  # optimisation has to proceed sequentially (compression with one set of parameters at a time),
  # so don't distribute the tasks for this
  original_sizes = list(map(corpus_size, fnames))
  try:
    def ppm(x):
      if legal_parameters(x):
        (a, b) = x
        work = [my_compressor.s(fname, paranoia, prior, ['ppm:d={0}:a={1}:b={2}'.format(int(depth),a,b)])
                for fname in fnames]
        sizes = celery.group(work)().get()
        return mean_effectiveness(sizes, original_sizes)
      else:
        return float('inf')
    opt = optimize.minimize(fun=ppm, args=(), x0=initial_guess, method=method,
                            options={'maxfev': 100, 'ftol': 0.0005, 'disp': True})
    # can't pickle opt, extract the useful results
    optd = {
      'x': opt.x,
      'fun': opt.fun,
      'success': opt.success,
      'status': opt.status,
      'message': opt.message,
      'nfev': opt.nfev,
      'nit': opt.nit,
    }
    return (True, optd)
  except Exception as e:
    print("ppm_minimize: exception occurred: " + traceback.format_exc())
    return (False, e)

def create_range(start, stop, N):
  return start + np.arange(0, N) * (stop - start) / (N - 1)

def legal_parameters(x):
  a, b = x
  # mathematically legal if >0, but set 0.01 threshold for numerical stability
  return 0 <= b <= 1 and a + b >= 0.01

# This function isn't memoised, as it's a function of memoized values, and the result size is large.
def optimise_brute(fnames, paranoia, prior, depth, alpha_range, beta_range, granularity):
  alphas = create_range(alpha_range[0], alpha_range[1], granularity)
  betas = create_range(beta_range[0], beta_range[1], granularity)
  grid = filter(legal_parameters, itertools.product(alphas, betas))

  original_sizes = list(map(corpus_size, fnames))

  # SOMEDAY: this would be more efficient using chunks, but can't get it to work with chaining
  work = [my_compressor.s(fname, paranoia, prior, ['ppm:d={0}:a={1}:b={2}'.format(int(depth),a,b)])
          for (a,b) in grid for fname in fnames]
  raw_res = celery.group(work)().get()

  res = np.empty((granularity, granularity))
  k = 0
  num_files = len(fnames)
  for i, a in enumerate(alphas):
    for j, b in enumerate(betas):
      if legal_parameters((a, b)):
        res[i][j] = mean_effectiveness(raw_res[k:k+num_files], original_sizes)
        k += num_files
      else:
        res[i][j] = np.inf

  beta_grid, alpha_grid = np.meshgrid(betas, alphas)
  optimum_i = np.argmin(res)
  optimum_alpha = alpha_grid.flatten()[optimum_i]
  optimum_beta = beta_grid.flatten()[optimum_i]
  min_val = res.flatten()[optimum_i]

  optimum = (optimum_alpha, optimum_beta), min_val
  evals = (alpha_grid, beta_grid), res

  return optimum, evals

# This function needn't be memoized, as it's a function of memoized values, but it does save time
# (at the cost of slightly inflating the cache).
@memo
def contour_data(prior, paranoia, depth, alpha_range, beta_range, granularity, fnames):
  optimum, evals = optimise_brute(fnames, paranoia, prior, depth,
                                  alpha_range, beta_range, granularity)
  opt_success, opt_res = ppm_minimize(fnames, paranoia, prior, depth, optimum[0])
  if opt_success:
    if opt_res['status'] != 0:
      print('ppm_contour_plot_helper: warning, abnormal termination of minimize, ' +
            'result may be inaccurate: ' + opt_res.message)
    optimum = opt_res['x'], opt_res['fun']
  else:
    print('ppm_contour_plot_helper: warning, error in ppm_minimize ' +
          '-- falling back to grid search: ' + str(opt_res))

  return (optimum, evals)

# This function needn't be memoized, as it's a function of memoized values, but it does save time.
# The result size is small, so the cache size doesn't increase much.
@memo
def ppm_find_optimal_alpha_beta(fnames, paranoia, prior, depth, granularity, alpha_range, beta_range):
  initial_guess = (0, 0.5) # PPMD

  if granularity > 1:
    res = optimise_brute(fnames, paranoia, prior, depth, alpha_range, beta_range, granularity)
    optimum, evals = res
    initial_guess, _ = optimum

  opt_success, opt_res = ppm_minimize(fnames, paranoia, prior, depth, initial_guess)
  if opt_success:
    status = "Normal" if opt_res['status'] == 0 else opt_res['message']
    return (opt_res['x'], opt_res['fun'], status)
  else:
    print('ppm_find_optimal_alpha_beta: error in ppm_minimize: ' + str(opt_res))