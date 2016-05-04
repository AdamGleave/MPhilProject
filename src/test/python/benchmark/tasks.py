import filecmp, itertools, tempfile, os, select, subprocess, sys
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

def build_compressor(standard_args, compress_args, decompress_args):
  def run_compressor(in_fname, out_fname, mode):
    args = standard_args.copy()
    if mode == CompressionMode.compress:
      args += compress_args
    else:
      args += decompress_args
    with open(in_fname, 'rb') as in_file:
      with open(out_fname, 'wb') as out_file:
        subprocess.check_call(args, stdin=in_file, stdout=out_file)
  return run_compressor

def compressed_filesize(compressor, input_fname, paranoia):
  with tempfile.NamedTemporaryFile(prefix='compression_en') as compressed:
    input_fname = os.path.join(config.CORPUS_DIR, input_fname)
    compressor(input_fname, compressed.name, CompressionMode.compress)
    if paranoia:
      with tempfile.NamedTemporaryFile(prefix='compression_de') as decompressed:
        compressor(compressed.name, decompressed.name, CompressionMode.decompress)
        if not filecmp.cmp(input_fname, decompressed.name):
          return "ERROR: decompressed file differs from original"
    return os.path.getsize(compressed.name)

@app.task
@memo
def ext_compressor(fname, paranoia, name):
  standard_args, compressor_args, decompressor_args = config.EXT_COMPRESSORS[name]
  compressor = build_compressor(standard_args, compressor_args, decompressor_args)
  return compressed_filesize(compressor, fname, paranoia)

sbt_classpath_cache = None
def find_sbt_classpath():
  global sbt_classpath_cache
  if not sbt_classpath_cache:
    classpath_cache = os.path.join(config.OUTPUT_DIR, 'classpath.cached')

    if os.path.exists(classpath_cache):
      with open(classpath_cache, 'r') as f:
        sbt_classpath_cache = f.read().strip()
    else:
      cwd = os.getcwd()
      os.chdir(config.PROJECT_DIR)
      res = subprocess.check_output(['sbt', 'export compile:fullClasspath'])
      os.chdir(cwd)

      sbt_classpath_cache = res.splitlines()[-1].decode("utf-8")

      with open(classpath_cache, 'w') as f:
        f.write(sbt_classpath_cache)
  return sbt_classpath_cache

def my_compressor_start_args(classname):
  classpath = find_sbt_classpath() + ':' + config.BIN_DIR
  class_qualified = 'uk.ac.cam.cl.arg58.mphil.compression.' + classname
  return ['java', '-Xms1024M', '-Xmx2048M',
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
    compressor = build_compressor(starting_args,
                                  ['compress'] + ending_args,
                                  ['decompress'] + ending_args)
    return compressor(in_file, out_file, mode)
  return run_compressor

def run_multicompressor():
  args = my_compressor_start_args('MultiCompressor')
  # use line buffer
  return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          universal_newlines=True, bufsize=1)

multi_compressor = None
#SOMEDAY: if result is in cache, quicker to hit DB locally rather than farming it out via Celery.
@app.task
@memo
def my_compressor(fname, paranoia, base, algorithms):
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

    cmd =  'measure {0} '.format(os.path.join(config.CORPUS_DIR, fname))
    cmd += ' '.join(my_compressor_end_args(base, algorithms)) + '\n'
    multi_compressor.stdin.write(cmd)

    ready_read = []
    waiting_for = 0
    timeout = 5.0
    while not ready_read:
      ready_read, _, _ = select.select([multi_compressor.stdout], [], [], timeout)
      if not ready_read:
        waiting_for += timeout
        timeout *= 2
        print("Waiting on multi_compressor ({0} with {1} on {2}) for {3}s"
              .format(base, algorithms, fname, waiting_for), file=sys.stderr)

    out = multi_compressor.stdout.readline().strip()

    prefix = 'BITS WRITTEN: '
    if out.find(prefix) != 0:
      raise RuntimeError("Unexpected output from MultiCompressor: '" + out + "'")
    bits = int(out[len(prefix):])
    return bits / 8 # compressed filesize in bytes

# The below functions aren't memoized, as the individual values are memoized
@app.task
def optimise_brute_callback(res, alphas, betas, granularity):
  results = np.empty((granularity, granularity))
  k = 0
  for i, a in enumerate(alphas):
    for j, b in enumerate(betas):
      if legal_parameters((a, b)):
        results[i][j] = res[k]
        k += 1
      else:
        results[i][j] = np.inf

  beta_grid, alpha_grid = np.meshgrid(betas, alphas)
  optimum_i = np.argmin(results)
  optimum_alpha = alpha_grid.flatten()[optimum_i]
  optimum_beta = beta_grid.flatten()[optimum_i]
  min_val = results.flatten()[optimum_i]

  optimum = (optimum_alpha, optimum_beta), min_val
  evals = (alpha_grid, beta_grid), results

  return optimum, evals

def create_range(start, stop, N):
  return start + np.arange(0, N) * (stop - start) / (N - 1)

def legal_parameters(x):
  a, b = x
  return a + b >= 0.01 # mathematically legal if >0, but set 0.01 threshold for numerical stability

def optimise_brute(fname, paranoia, prior, depth, alpha_range, beta_range, granularity):
  alphas = create_range(alpha_range[0], alpha_range[1], granularity)
  betas = create_range(beta_range[0], beta_range[1], granularity)
  grid = filter(legal_parameters, itertools.product(alphas, betas))

  # SOMEDAY: this would be more efficient using chunks, but can't get it to work with chaining
  work = [my_compressor.s(fname, paranoia, prior, ['ppm:d={0}:a={1}:b={2}'.format(int(depth),a,b)])
          for (a,b) in grid]
  return celery.chord(work, optimise_brute_callback.s(alphas, betas, granularity))

@app.task
def ppm_minimize(fname, paranoia, prior, depth, initial_guess, method='Nelder-Mead'):
  # optimisation has to proceed sequentially (compression with one set of parameters at a time),
  # so don't distribute the tasks for this
  def ppm(x):
    (a, b) = x
    return my_compressor(fname, paranoia, prior, ['ppm:d={0}:a={1}:b={2}'.format(int(depth),a,b)])
  return optimize.minimize(fun=ppm,
                           args=(),
                           x0=initial_guess,
                           method=method)
