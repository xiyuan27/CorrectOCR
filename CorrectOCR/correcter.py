import cmd
import csv
import logging
from collections import defaultdict

from . import punctuationRE, open_for_reading, splitwindow, ensure_new_file
from .workspace import Workspace

'''
IMPORTANT BEFORE USING:
To display interactive text your environment must be compatible with the encoding.
For example:
> export LANG=is_IS.UTF-8
> export LC_ALL=is_IS.UTF-8
> locale
> export PYTHONIOENCODING=utf8
'''


class Correcter(object):
	def __init__(self, dictionary, heuristics, memos, caseInsensitive=False, k=4):
		self.caseInsensitive = caseInsensitive
		self.memos = memos
		self.k = k
		self.log = logging.getLogger(f'{__name__}.Correcter')
		self.dictionary = dictionary
		self.heuristics = heuristics
	
	# remove selected hyphens from inside a single token - postprocessing step
	def dehyph(self, tk):
		o = tk
		# if - in token, and token is not only punctuation, and - is not at end or start of token:
		if (u'-' in tk) & ((len(punctuationRE.sub('', tk)) > 0) & ((tk[-1] != u'-') & (tk[0] != u'-'))):
			# if - doesn't precede capital letter, and the word including dash form isn't in dictionary:
			if ((not tk[tk.index(u'-')+1].isupper()) & ((not tk in dws) & (not tk.lower() in dws))):
				# if the word not including dash form is in dictionary, only then take out the dash
				if ((punctuationRE.sub('', tk) in dws) or (punctuationRE.sub('', tk).lower() in dws)):
					o = tk.replace(u'-', u'')
		return(o)
	
	# try putting together some lines that were split by hyphenation - preprocessing step
	def linecombiner(self, ls):
		for i in range(len(ls) - 2):
			if (ls[i] != u'BLANK'):
				#self.log.debug(ls[i])
				curw = ls[i].original
				newl = ls[i+1].original
				nexw = ls[i+2].original
				# look for pattern: wordstart-, newline, restofword.
				
				if (((newl == u'_NEWLINE_N_') or (newl == u'_NEWLINE_R_')) & ((curw[-1] == u'-') & (len(curw) > 1))):
					# check that: wordstart isn't in dictionary,
					# combining it with restofword is in dictionary,
					# and restofword doesn't start with capital letter
					# -- this is generally approximately good enough
					if (not punctuationRE.sub('', curw) in self.dictionary
						and punctuationRE.sub('', curw+nexw) in self.dictionary
						and nexw[0].islower()):
						# make a new row to put combined form into the output later
						ls[i] = {
							'Original': curw[:-1]+nexw,
							'1-best': curw[:-1]+nexw,
							'1-best prob.': 0.99,
							'2-best': '_PRE_COMBINED_',
							'2-best prob.': 1.11e-25,
							'3-best': '_PRE_COMBINED_',
							'3-best prob.': 1.11e-25,
							'4-best': '_PRE_COMBINED_',
							'4-best prob.': 1.11e-25,
						}
						ls[i+1] = {'Original': 'BLANK'}
						ls[i+2] = {'Original': 'BLANK'}
		return [lin for lin in ls if lin != u'BLANK']
	
	def evaluate(self, token):
		#self.log.debug(token)
		
		# this should not happen in well-formed input
		if len(token.original) == 0:
			return ('error', f'Input is malformed! Original is 0-length: {token}')
		
		# catch linebreaks
		if (token.original in [u'_NEWLINE_N_', u'_NEWLINE_R_']):
			return ('linefeed', None)
		
		# catch memorised corrections
		if not token.is_punctuation() and token.original in self.memos:
			return ('memoized', self.memos[token.original])
		
		# k best candidate words
		filtids = [k for k, (c,p) in token.kbest() if c in self.dictionary]
		
		(decision, token.bin) = self.heuristics.evaluate(token)
		#self.log.debug(f'{bin} {dcode}')
		
		# return decision codes and output token form or candidate list as appropriate
		if decision == 'o':
			return ('original', token.original)
		elif decision == 'k':
			return ('kbest', 1)
		elif decision == 'd':
			return ('kdict', filtids[0])
		else:
			# decision is 'a' or unrecognized
			return ('annotator', filtids)


class CorrectionShell(cmd.Cmd):
	prompt = 'CorrectOCR> '
	
	def start(tokens, dictionary, correctionTracking, intro=None):
		sh = CorrectionShell()
		sh.tokenwindow = splitwindow(tokens, before=7, after=7)
		sh.dictionary = dictionary
		sh.tracking = {
			'tokenCount': 0,
			'humanCount': 0,
			'tokenTotal': len(tokens),
			'newWords': [],
			'correctionTracking': correctionTracking,
		}
		sh.log = logging.getLogger(f'{__name__}.CorrectionShell')
		sh.use_rawinput = True
		
		sh.cmdloop(intro)

		return sh.tracking
	
	def preloop(self):
		return self.nexttoken()
	
	def nexttoken(self):
		try:
			ctxl, self.token, ctxr = next(self.tokenwindow)
			if self.token.gold:
				return self.nexttoken()
			(self.decision, self.selection) = (self.token.bin['decision'], self.token.bin['selection'])
			
			self.tracking['tokenCount'] += 1
			if self.decision == 'annotator':
				self.tracking['humanCount'] +=1 # increment human-effort count
				
				left = ' '.join([c.gold or c.original for c in ctxr])
				right = ' '.join([c.original for c in ctxl])
				print(f'\n\n...{left} \033[1;7m{self.token.original}\033[0m {right}...\n')
				print(f'\nSELECT for {self.token.original} :\n')
				for k, (candidate, probability) in self.token.kbest():
					inDict = ' * is in dictionary' if k in self.selection else ''
					print(f'\t{k}. {candidate} ({probability}){inDict}\n')
				
				self.prompt = f"CorrectOCR {self.tracking['tokenCount']}/{self.tracking['tokenTotal']} ({self.tracking['humanCount']}) > "
			else:
				self.cmdqueue.insert(0, f'{self.decision} {self.selection}')
		except StopIteration:
			print('Reached end of tokens, going to quit...')
			return self.onecmd('quit')
	
	def select(self, word, decision, save=True):
		print(f'Selecting {decision} for "{self.token.original}": "{word}"')
		self.token.gold = word
		if save:
			cleanword = punctuationRE.sub('', word)
			if cleanword not in self.dictionary:
				self.tracking['newWords'].append(cleanword) # add to suggestions for dictionary review
			self.dictionary.add(cleanword) # add to current dictionary for subsequent heuristic decisions
			if f'{self.token.original}\t{cleanword}' not in self.tracking['correctionTracking']:
				self.tracking['correctionTracking'][f'{self.token.original}\t{cleanword}'] = 0
			self.tracking['correctionTracking'][f'{self.token.original}\t{cleanword}'] += 1
		return self.nexttoken()
	
	def emptyline(self):
		if self.lastcmd == 'original':
			return super().emptyline() # repeats by default
		else:
			pass # dont repeat other commands
	
	def do_original(self, arg):
		"""Choose original (abbreviation: o)"""
		return self.select(self.token.original, 'original')
	
	def do_shell(self, arg):
		"""Custom input to replace token"""
		return self.select(arg, 'user input')
	
	def do_kbest(self, arg):
		"""Choose k-best by number (abbreviation: just the number)"""
		if arg:
			k = int(arg[0]) 
		else:
			k = 1
		(candidate, _) = self.token.kbest(k)
		return self.select(candidate, f'{k}-best')
	
	def do_kdict(self, arg):
		"""Choose k-best which is in dictionary"""
		(candidate, _) = self.token.kbest(int(arg))
		return self.select(candidate, f'k-best from dict')
	
	def do_memoized(self, arg):
		return self.select(arg, 'memoized correction', save=False)
	
	def do_error(self, arg):
		self.log.error(f'ERROR: {arg} {self.token}')
	
	def do_linefeed(self, arg):
		return self.select('\n', 'linefeed', save=False)
	
	def do_defer(self, arg):
		"""Defer decision for another time."""
		print('Deferring decision...')
		return self.nexttoken()
	
	def do_quit(self, arg):
		return True
	
	def default(self, line):
		if line == 'o':
			return self.onecmd('original')
		elif line == 'k':
			return self.onecmd('kbest 1')
		elif line.isnumeric():
			return self.onecmd(f'kbest {line}')
		elif line == 'q':
			return self.onecmd('quit')
		elif line == 'p':
			print(self.decision, self.selection, self.token) # for debugging
		else:
			self.log.error(f'bad command: "{line}"')
			return super().default(line)
