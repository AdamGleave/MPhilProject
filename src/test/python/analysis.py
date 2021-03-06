#!/usr/bin/env python3

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


import argparse, csv, functools, itertools, math, os

import numpy as np

import benchmark.general as general
import benchmark.plot as plot
import benchmark.config_analysis as config

import matplotlib.pyplot as plt
import prettyplotlib as ppl

def save_table(test, result):
  os.makedirs(config.LATEX_OUTPUT_DIR, exist_ok=True)
  with open(os.path.join(config.LATEX_OUTPUT_DIR, test + '.tex'), 'w') as out:
    out.write(result)

def load_benchmark(fname):
  with open(fname) as dataf:
    reader = csv.DictReader(dataf)
    effectiveness = {}
    filesizes = {}
    for row in reader:
      file = row['File']
      filesize = int(row['Size'])
      del row['File']
      del row['Size']
      effectiveness[file] = {}
      for k, size in row.items():
        try:
          effectiveness[file][k] = float(size) / filesize * 8
        except ValueError:
          effectiveness[file][k] = float('inf')
      filesizes[file] = filesize
    return (effectiveness, filesizes)

def load_resources(fname):
  def update(d, file, compressor, val):
    by_file = d.get(file, {})
    by_compressor = by_file.get(compressor, [])
    by_compressor.append(val)
    by_file[compressor] = by_compressor
    d[file] = by_file
  with open(fname) as dataf:
    reader = csv.DictReader(dataf)
    runtimes = {}
    memories = {}
    for row in reader:
      file = row['file']
      compressor = row['compressor']
      runtime = float(row['wall_runtime'])
      memory = int(row['memory'])

      update(runtimes, file, compressor, runtime)
      update(memories, file, compressor, memory)
  return (runtimes, memories)

def load_parameters(fname, algos):
  with open(fname) as dataf:
    reader = csv.DictReader(dataf)
    res = {}
    for row in reader:
      depth = int(row['depth'])
      for algo in algos:
        by_algo = res.get(algo, {})
        alpha = float(row[algo + '_alpha'])
        beta = float(row[algo + '_beta'])
        efficiency = float(row[algo + '_mean_efficiency'])
        by_algo[depth] = (alpha, beta, efficiency)
        res[algo] = by_algo
  return res

def process_score_settings(raw):
  res = dict(raw)
  res['groups'] = None
  res['sizes'] = res.get('sizes', True)
  res['omit_files_col'] = res.get('omit_files_col', False)
  res['files_last'] = res.get('files_last', False)
  res['label_groups'] = res.get('label_groups', False)
  res['column_type'] = res.get('column_type', config.default_column_type)
  res['padding'] = res.get('padding', config.default_padding)
  res['font'] = res.get('font', config.default_font)
  if type(res['algos'][0]) == tuple:
    algo_groups = res['algos']
    flat_algos = []
    groups = []
    for group, algos in algo_groups:
      flat_algos += algos
      groups.append((group, len(algos)))
    res['algos'] = flat_algos
    res['groups'] = groups
  return res

def generate_row(row):
  return ' & '.join(row) + r"\\"

def autoscale(settings, data):
  subset = []
  for _group, files in settings['files'].items():
    for f in files:
      for a in settings['algos']:
        if math.isfinite(data[f][a]):
          subset.append(data[f][a])
  return min(subset), max(subset)

def effectiveness_format(x, is_best, scale, leading, font, fg_cm, bg_cm):
  smallest, largest = scale

  if x == float('inf'): # failure
    bg, fg = config.ERROR_COLOUR
    val = '\hspace{0.39em}fail\hspace{0.45em}'
  else:
    p = (x - smallest) / (largest - smallest)
    if p < 0 or p > 1:
      print("WARNING: value {0} outside {1}".format(x, scale))
    bg = bg_cm(p)
    fg = fg_cm(p)
    val = '{0:.3f}'.format(x)
    pad = leading + 4 - len(val)
    val = '\hspace{0.5em}' * pad + val

  if is_best:
    bold_kern = config.FONT_BOLD_ADJUSTMENTS[font]
    val = r'\textbf{' + val + r'}'
    if bold_kern:
      val = r'\kern-' + bold_kern + r'em' + val + r'\kern-' + bold_kern + 'em'
  return r'{\kern-0.35em\colorbox[rgb]{' + '{0},{1},{2}'.format(*bg) + r'}{\textcolor[rgb]{' + \
         '{0},{1},{2}'.format(*fg) + r'}{' + val + '}}\kern-0.35em}'

def generate_score_table(test, settings, data):
  effectiveness, filesizes = data
  settings = process_score_settings(settings)
  if 'scale' in settings:
    scale = settings['scale']
  else:
    scale = autoscale(settings, effectiveness)

  res = []
  res.append(r'\setlength{\tabcolsep}{1ex}')
  res.append(r'\hspace{' + config.label_groups_shift + r'}')

  algos = settings['algos']
  cols = ''
  for algo in algos:
    cols += settings['column_type'](algo)
  if not settings['omit_files_col']:
    if settings['sizes']:
      filecols = 'lr'
    else:
      filecols = 'l'
    if settings['files_last']:
      cols += filecols
    else:
      cols = filecols + cols
  if settings['label_groups']:
    cols = 'm{0em}' + cols
  res.append(r'\begin{tabular}{' + cols + '}')
  res.append(r'\toprule')

  if settings['groups']:
    group_row = []
    for group, num_algos in settings['groups']:
      group_row.append(r'\multicolumn{' + str(num_algos) + r'}{c}{' + group + r'}')
    if not settings['omit_files_col']:
      headings = [r'\textbf{File}']
      if settings['sizes']:
        headings.append(r'\multicolumn{1}{c}{\textbf{Size}}')
      if settings['label_groups']:
        headings = [''] + headings
      if settings['files_last']:
        group_row = group_row + headings
      else:
        group_row = headings + group_row
    res.append(generate_row(group_row))

  algo_row = []
  algo_abbreviations = settings.get('algo_abbreviations', config.ALGO_ABBREVIATIONS)
  for algo in algos:
    abbrev =  algo_abbreviations[algo]
    left_padding, right_padding = settings['padding'](algo)
    abbrev = left_padding + abbrev + right_padding
    algo_row.append(abbrev)

  if not settings['omit_files_col']:
    headings2 = [r'']
    if settings['label_groups']:
      headings2 = [''] + headings2
    if settings['sizes']:
      headings2.append(r'\multicolumn{1}{c}{\textbf{(KiB)}}')

    if settings['files_last']:
      headings2.reverse()
      algo_row += headings2
    else:
      algo_row = headings2 + algo_row
  res.append(generate_row(algo_row))
  res.append(r'\midrule')

  for file_group, files in settings['files'].items():
    if settings['label_groups']:
      label = r'\multirow{' + str(len(files)) + r'}' + \
              r'{*}{\hspace{-' + config.label_groups_shift + r'}\rotatebox{65}{\emph{' + file_group + r'}~}}'
      res.append(label)
    for file in files:
      filedata = effectiveness[file]
      file_abbrev = r'\code{' + config.FILE_ABBREVIATIONS[file] + r'}'
      size = r'\num{' + '{0:.0f}'.format(filesizes[file] / 1024.0) + r'}'
      row = []
      best = min([filedata[a] for a in algos])
      for algo in algos:
        efficiency = filedata[algo]
        is_best = efficiency == best
        leading = config.get_leading(algo)
        val = effectiveness_format(efficiency, is_best, scale, leading, settings['font'],
                                   config.FG_COLORMAP, config.BG_COLORMAP)
        row.append(val)

      preface = [file_abbrev]
      if settings['label_groups']:
        preface = [''] + preface
      if settings['sizes']:
        preface.append(size)
      if not settings['omit_files_col']:
        if settings['files_last']:
          preface.reverse()
          row += preface
        else:
          row = preface + row
      res.append(generate_row(row))
    res.append(r'\addlinespace[0em]\midrule\addlinespace[0.1em]')

  res[-1] = r'\bottomrule'
  res.append(r'\end{tabular}')

  out = '\n'.join(res)
  save_table(test, out)

def generate_score_table_files_col(test, settings, data):
  effectiveness, filesizes = data
  if 'scale' in settings:
    scale = settings['scale']
  else:
    scale = autoscale(settings, effectiveness)
  algos = settings['algos']
  files = []
  for file_group, files_in_group in settings['files'].items():
    files += files_in_group

  res = []
  res.append(r'\setlength{\tabcolsep}{1ex}')

  cols = 'l' * (len(files) + 1)
  res.append(r'\begin{tabular}{' + cols + '}')
  res.append(r'\toprule')

  heading = [r'\textbf{Method}']
  for file in files:
    file_abbrev = config.FILE_ABBREVIATIONS[file]
    heading.append(r'\tilt{' + file_abbrev + r'}')
  res.append(generate_row(heading))
  res.append(r'\midrule')

  for algo in algos:
    algo_abbrev = config.ALGO_ABBREVIATIONS[algo]
    row = [algo_abbrev]
    for file in files:
      filedata = effectiveness[file]
      efficiency = filedata[algo]
      val = effectiveness_format(efficiency, False, scale, 1, config.default_font,
                                 config.FG_COLORMAP, config.BG_COLORMAP)
      row.append(val)
    res.append(generate_row(row))

  res.append(r'\bottomrule')
  res.append(r'\end{tabular}')

  out = '\n'.join(res)
  save_table(test, out)

def generate_score_bar(test, settings, data):
  granularity = float(settings['granularity'])
  space = 1 / granularity * settings['width']
  space_str = r'\kern-0.25em\hspace*{' + \
              str(space) + \
              r'\textwidth}\kern-0.25em'

  out = ''
  for x in range(settings['granularity']):
    p = 1 - x / granularity
    fg = config.FG_COLORMAP(p)
    bg = config.BG_COLORMAP(p)

    out += r'{\colorbox[rgb]{' + '{0},{1},{2}'.format(*bg) + r'}{\textcolor[rgb]{' + \
             '{0},{1},{2}'.format(*fg) + r'}{' + space_str + r'}}}'

  def generate_arrow(left):
    if left:
      inner = r'\textsf{worse} $\leftarrow$'
    else:
      inner = r'$\rightarrow$ \textsf{better}'
    out = r'\raisebox{-0.25em}{' + inner + r'}'
    if left:
      out = out + '\hspace{0.5em}'
    else:
      out = '\hspace{0.5em}' + out
    return out
  out = generate_arrow(True) + out + generate_arrow(False)

  save_table(test, out)

def mean_effectiveness(data, files, algo):
  return np.mean([data[file][algo] for file in files])

def generate_score_summary(test, settings, data):
  algos = settings['algos']
  algo_abbreviations = settings.get('algo_abbreviations', config.ALGO_ABBREVIATIONS)

  res = []
  # stretch table to fill width of page
  res.append(r'\begin{tabular*}{\columnwidth}{l@{\extracolsep{\stretch{1}}}' + len(algos)*'c' + r'@{}}')
  res.append(r'\toprule')

  algo_row = [r'\textbf{Group}']
  for algo in algos:
    abbrev = algo_abbreviations[algo]
    algo_row.append(abbrev)
  res.append(generate_row(algo_row))
  res.append(r'\midrule')

  files = settings['files']
  overall = settings.get('overall', False)
  if overall:
    files = files.copy()
    files['All'] = list(itertools.chain(*files.values()))
  for group_name, group_files in files.items():
    efficiencies = [mean_effectiveness(data, group_files, algo) for algo in algos]

    best = np.min(efficiencies)
    row = [group_name]
    for x in efficiencies:
      val = '{0:.3f}'.format(x)
      if x == best:
        val = r'\textbf{' + val + r'}'
      row.append(val)
    res.append(generate_row(row))

  res.append(r'\bottomrule')
  res.append(r'\end{tabular*}')

  out = '\n'.join(res)
  save_table(test, out)

def generate_parameter_table(test, settings, data):
  algos = settings['algos']
  files = settings['files']
  test_parameters = load_parameters(settings['test_parameters'], algos.keys())

  res = []
  # stretch table to fill width of page
  res.append(r'\begin{tabular*}{\columnwidth}{ll@{\extracolsep{\stretch{1}}}*{5}{c}@{}}')
  res.append(r'\toprule')
  res.append(r'\textbf{Alphabet} & \textbf{Prior} & \textbf{$\boldsymbol{\bar{e}}^\text{TE}$} & \textbf{$\boldsymbol{\bar{e}}^\text{TR}$} & \textbf{$\boldsymbol{\Delta \bar{e}}$} & \textbf{due to $\mathbf{d}$} & \textbf{due to $\boldsymbol{\alpha,\beta}$} \\')
  res.append(r'\midrule')

  def mean_format(x):
    return '{0:1.3f}'.format(x)
  def delta_format(x):
    # exponential_notation = '{0:.2E}'.format(x)
    # leading, exponent = exponential_notation.split("E")
    # return '${0}\\times 10^{{{1}}}$'.format(leading, exponent)
    return '{0:1.5f}'.format(x)

  for algo, (alphabet, prior) in algos.items():
    by_algo = test_parameters[algo]
    def comparator(x):
      depth, (alpha, beta, mean_effectiveness) = x
      return mean_effectiveness
    best_parameters = min(by_algo.items(), key=comparator)
    best_depth = best_parameters[0]

    cols = ['ppm_test_group_opt_{0}'.format(algo),
            'ppm_training_group_opt_{0}'.format(algo),
            'ppm_training_group_{0}_{1}'.format(best_depth, algo)
           ]
    means = map(functools.partial(mean_effectiveness, data, files), cols)
    test_mean, train_mean, train_on_test_depth_mean = means

    delta = train_mean - test_mean
    delta_due_to_ab = train_on_test_depth_mean - test_mean
    delta_due_to_depth = delta - delta_due_to_ab

    mean_formatted = map(mean_format, [test_mean, train_mean])
    delta_formatted = map(delta_format, [delta, delta_due_to_depth, delta_due_to_ab])
    row = generate_row([alphabet, prior] + list(mean_formatted) + list(delta_formatted))
    res.append(row)

  res.append(r'\bottomrule')
  res.append(r'\end{tabular*}')

  out = '\n'.join(res)
  save_table(test, out)

def confidence_interval(vals, alpha):
  import numpy as np
  import math
  mean = np.mean(vals)
  sd = np.std(vals)

  return (mean, sd/math.sqrt(len(vals)))

def process_resource_data(settings, data):
  runtimes, memories = data
  col = settings['col']

  if col == 'runtime':
    resources = runtimes
    format = lambda s : general.human_readable_time_latex(s * 1000)
  elif col == 'memory':
    resources = {k1: {k2: [x * 1024 for x in v2] for k2, v2 in v1.items()} for k1, v1 in memories.items()}
    format = lambda s : general.human_readable_size_latex(s)
  else:
    assert(False)

  return resources, format

def generate_resource_table(test, settings, data):
  resources, format = process_resource_data(settings, data)
  files = settings['files']
  algos = settings['algos']

  res = []
  res.append(r'\begin{tabular}{l' + 'l'*len(algos) + '}')
  res.append(r'\toprule')

  file_row = [r'\textbf{File}']
  for file in files:
    abbrev = config.FILE_ABBREVIATIONS[file]
    file_row.append(abbrev)
  res.append(generate_row(file_row))
  res.append(r'\midrule')

  for algo in algos:
    algo_abbrev = config.ALGO_ABBREVIATIONS[algo]
    row = [algo_abbrev]

    for file in files:
      vals = resources[file][algo]
      mean, uncertainty = confidence_interval(vals, config.RESOURCE_ALPHA)
      print("{0}: {1}".format(algo, mean))
      row.append("${0} \pm {1}$".format(format(mean), format(uncertainty)))
    res.append(generate_row(row))
  res.append(r'\bottomrule')
  res.append(r'\end{tabular}')

  out = '\n'.join(res)
  save_table(test, out)

def generate_resource_figure(test, settings, data):
  file = settings['file']
  algos = settings['algos']
  resources, format = process_resource_data(settings, data)
  resources = resources[file]

  # flatten data
  flattened = [confidence_interval(resources[algo], config.RESOURCE_ALPHA) for algo in algos]
  x = np.arange(len(algos))
  y = [z[0] for z in flattened]
  yerr = [z[1] for z in flattened]

  algo_abbreviations = settings.get('algo_abbreviations', config.ALGO_ABBREVIATIONS)
  xticks = list(map(algo_abbreviations.get, algos))
  num_colors = max(3, len(algos)) # brewer needs at least 3 colors
  colors = ppl.brewer2mpl.get_map('Set2', 'qualitative', num_colors).mpl_colors[:len(algos)]

  if settings['style']:
    plot.set_style(settings['style'])
  plot.new_figure()
  fig, ax = plt.subplots()
  
  scale = settings.get('scale', 'log')
  log_scale = scale == 'log'
  height = y
  if settings['col'] == 'memory' and scale == 'mebi': # in megabytes
      height = [x / (1024 * 1024) for x in y]
      yerr = [x / (1024 * 1024) for x in yerr]

  rects = ppl.bar(x, height, xticklabels=xticks, yerr=yerr, log=log_scale, grid='y', color=colors)

  # Annotate
  for rect, y in zip(rects, y):
    bar_x = rect.get_x() + rect.get_width()/2.
    bar_y = rect.get_height()

    label = format(y)
    fontsize = settings.get('barlabel_fontsize')
    plt.annotate(label, xy=(bar_x, bar_y), xytext=(0, 10), textcoords='offset points',
                 horizontalalignment='center', verticalalignment='bottom', fontsize=fontsize)

  plt.xlabel('Compressor')
  labelpad = settings.get('labelpad', None)
  if settings['col'] == 'runtime':
    plt.ylabel(r'Runtime (\si{\second})', labelpad=labelpad)
  elif settings['col'] == 'memory':
    prefix = r'\mebi' if scale == 'mebi' else ''
    plt.ylabel(r'Memory (\si{' + prefix + r'\byte})', labelpad=labelpad)
    if log_scale:
        ax.set_yscale('log', basey=2) # units are in powers of two, so scale should be as well

    # hack to make labels fit
    ymin, ymax = plt.ylim()
    plt.ylim((ymin, ymax*2))

  plot.save_figure(fig, 'resources', [test])

verbose = False

def main():
  description = "Produce LaTeX tables"
  parser = argparse.ArgumentParser(description=description)
  parser.add_argument('--verbose', dest='verbose', action='store_true',
                      help='produce detailed output showing work performed.')
  parser.add_argument('tests', nargs='*',
                      help='list of analyses to perform')

  args = vars(parser.parse_args())
  global verbose
  verbose = args['verbose']

  tests = args['tests']
  if not tests:
    tests = config.TESTS.keys()

  score_data = load_benchmark(config.BENCHMARK_INPUT)
  resource_data = load_resources(config.RESOURCES_INPUT)

  for test in tests:
    if verbose:
      print("Generating " + test)
    settings = config.TESTS[test]
    type = settings['type']

    if type == 'score_full':
      data = score_data
      f = generate_score_table
    elif type == 'score_presentation':
      data = score_data
      f = generate_score_table_files_col
    elif type == 'score_bar':
      data = None
      f = generate_score_bar
    elif type == 'score_summary':
      data = score_data[0]
      f = generate_score_summary
    elif type == 'parameter_table':
      data = score_data[0]
      f = generate_parameter_table
    elif type == 'resource_table':
      data = resource_data
      f = generate_resource_table
    elif type == 'resource_figure':
      data = resource_data
      f = generate_resource_figure
    else:
      print("WARNING: unknown table type '{0}', skipping".format(type))
      continue
    f(test, settings, data)

if __name__ == "__main__":
  main()
