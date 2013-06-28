#!/usr/bin/env python
import sys
import os
import os.path
import time

from base64 import b64encode, b64decode
import re
import string

import lxml.etree
import lxml.objectify
import lxml.html
import lxml.html.clean
import lxml.builder

import urllib2
import socket
from cookielib import CookieJar
import chardet
import urlparse

from readability import readability

LIM_ITEM = 100	# deletes what's beyond
MAX_ITEM = 50	# cache-only beyond
MAX_TIME = 7	# cache-only after
DELAY = 10	# xml cache
TIMEOUT = 2	# http timeout

OPTIONS = ['progress', 'cache']

UA_RSS = 'Liferea/1.8.12 (Linux; fr_FR.utf8; http://liferea.sf.net/)'
UA_HML = 'Mozilla/5.0 (Macintosh; U; Intel Mac OS X 10.6; en-US; rv:1.9.2.11) Gecko/20101012 Firefox/3.6.11'

PROTOCOL = ['http', 'https', 'ftp']

ITEM_MAP = {
	'link':		(('{http://www.w3.org/2005/Atom}link', 'href'),	'{}link'),
	'desc':		('{http://www.w3.org/2005/Atom}summary',	'{}description'),
	'description':	('{http://www.w3.org/2005/Atom}summary',	'{}description'),
	'summary':	('{http://www.w3.org/2005/Atom}summary',	'{}description'),
	'content':	('{http://www.w3.org/2005/Atom}content',	'{http://purl.org/rss/1.0/modules/content/}encoded')
	}
RSS_MAP = {
	'desc':		('{http://www.w3.org/2005/Atom}subtitle',	'{}description'),
	'description':	('{http://www.w3.org/2005/Atom}subtitle',	'{}description'),
	'subtitle':	('{http://www.w3.org/2005/Atom}subtitle',	'{}description'),
	'item':		('{http://www.w3.org/2005/Atom}entry',		'{}item'),
	'entry':	('{http://www.w3.org/2005/Atom}entry',		'{}item')
	}

if 'REQUEST_URI' in os.environ:
	import httplib
	httplib.HTTPConnection.debuglevel = 1

	import cgitb
	cgitb.enable()

def log(txt):
	if not 'REQUEST_URI' in os.environ:
		if os.getenv('DEBUG', False):
			print repr(txt)
	else:
		with open('morss.log', 'a') as file:
			file.write(repr(txt).encode('utf-8') + "\n")

def cleanXML(xml):
	table = string.maketrans('', '')
	return xml.translate(table, table[:32]).lstrip()

def lenHTML(txt):
	if len(txt):
		return len(lxml.html.fromstring(txt).text_content())
	else:
		return 0

def parseOptions(available):
	options = None
	if 'REQUEST_URI' in os.environ:
		if 'REDIRECT_URL' in os.environ:
			url = os.environ['REQUEST_URI'][1:]
		else:
			url = os.environ['REQUEST_URI'][len(os.environ['SCRIPT_NAME'])+1:]

		if urlparse.urlparse(url).scheme not in PROTOCOL:
			split = url.split('/', 1)
			if len(split) and split[0] in available:
				options = split[0]
				url = split[1]
			url = "http://" + url

	else:
		if len(sys.argv) == 3:
			if sys.argv[1] in available:
				options = sys.argv[1]
			url = sys.argv[2]
		elif len(sys.argv) == 2:
			url = sys.argv[1]
		else:
			return (None, None)

		if urlparse.urlparse(url).scheme not in PROTOCOL:
			url = "http://" + url

	return (url, options)

class Cache:
	"""Light, error-prone caching system."""
	def __init__(self, folder, key):
		self._key = key
		self._hash = str(hash(self._key))

		self._dir = folder
		self._file = self._dir + "/" + self._hash

		self._cached = {} # what *was* cached
		self._cache = {} # new things to put in cache

		if os.path.isfile(self._file):
			data = open(self._file).readlines()
			for line in data:
				if "\t" in line:
					key, bdata = line.split("\t", 1)
					self._cached[key] = bdata

		log(self._hash)

	def __del__(self):
		self.save()

	def __contains__(self, key):
		return key in self._cached

	def get(self, key):
		if key in self._cached:
			self._cache[key] = self._cached[key]
			return b64decode(self._cached[key])
		else:
			return None

	def set(self, key, content):
		self._cache[key] = b64encode(content)

	def save(self):
		if len(self._cache) == 0:
			return

		out = []
		for (key, bdata) in self._cache.iteritems():
			out.append(str(key) + "\t" + bdata)
		txt = "\n".join(out)

		if not os.path.exists(self._dir):
			os.makedirs(self._dir)

		with open(self._file, 'w') as file:
			file.write(txt)

	def isYoungerThan(self, sec):
		if not os.path.exists(self._file):
			return False

		return time.time() - os.path.getmtime(self._file) < sec

class XMLMap(object):
	"""
	Sort of wrapper around lxml.objectify.StringElement (from which this
	class *DOESN'T* inherit) which makes "links" between different children
	of an element. For example, this allows cheap, efficient, transparent
	RSS 2.0/Atom seamless use, which can be way faster than feedparser, and
	has the advantage to edit the corresponding mapped fields. On top of
	that, XML output with "classic" lxml API calls (such as
	lxml.etree.tostring) is still possible. Element attributes are also
	supported (as in <entry attr='value'/>).

	However, keep in mind that this feature's support is only partial. For
	example if you want to alias an element to both <el>value</el> and <el
	href='value'/>, and put them as ('el', ('el', 'value')) in the _map
	definition, then only 'el' will be whatched, even if ('el', 'value')
	makes more sens in that specific case, because that would require to
	also check the others, in case of "better" match, which is not done now.

	Also, this class assumes there's some consistency in the _map
	definition. Which means that it expects matches to be always found in
	the same "column" in _map. This is useful when setting values which are
	not yet in the XML tree. Indeed the class will try to use the alias from
	the same column. With the RSS/Atom example, the default _map will always
	create elements for the same kind of feed.
	"""
	def __init__(self, obj, alias=ITEM_MAP, string=False):
		self._xml = obj
		self._key = None
		self._map = alias
		self._str = string

		self._guessKey()

	def _guessKey(self):
		for tag in self._map:
			self._key = 0
			for choice in self._map[tag]:
				if not isinstance(choice, tuple):
					choice = (choice, None)
				el, attr = choice
				if hasattr(self._xml, el):
					if attr is None:
						return
					else:
						if attr in self._xml[el].attrib:
							return
				self._key+=1
		self._key = 0

	def _getElement(self, tag):
		"""Returns a tuple whatsoever."""
		if tag in self._map:
			for choice in self._map[tag]:
				if not isinstance(choice, tuple):
					choice = (choice, None)
				el, attr = choice
				if hasattr(self._xml, el):
					if attr is None:
						return (self._xml[el], attr)
					else:
						if attr in self._xml[el].attrib:
							return (self._xml[el], attr)
			return (None, None)
		if hasattr(self._xml, tag):
			return (self._xml[tag], None)
		return (None, None)

	def __getattr__(self, tag):
		el, attr = self._getElement(tag)
		if el is not None:
			if attr is None:
				out = el
			else:
				out = el.get(attr)
		else:
			out = self._xml.__getattr__(tag)

		return unicode(out) if self._str else out

	def __getitem__(self, tag):
		if self.__contains__(tag):
			return self.__getattr__(tag)
		else:
			return None

	def __setattr__(self, tag, value):
		if tag.startswith('_'):
			return object.__setattr__(self, tag, value)

		el, attr = self._getElement(tag)
		if el is not None:
			if attr is None:
				if (isinstance(value, lxml.objectify.StringElement)
					or isinstance(value, str)
					or isinstance(value, unicode)):
					el._setText(value)
				else:
					el = value
				return
			else:
				el.set(attr, value)
				return
		choice = self._map[tag][self._key]
		if not isinstance(choice, tuple):
			child = lxml.objectify.Element(choice)
			self._xml.append(child)
			self._xml[choice] = value
			return
		else:
			el, attr = choice
			child = lxml.objectify.Element(choice, attrib={attr:value})
			self._xml.append(child)
			return

	def __contains__(self, tag):
		el, attr = self._getElement(tag)
		return el is not None

	def remove(self):
		self._xml.getparent().remove(self._xml)

	def tostring(self, **k):
		"""Returns string using lxml. Arguments passed to tostring."""
		out = self._xml if self._xml.getparent() is None else self._xml.getparent()
		return lxml.etree.tostring(out, pretty_print=True, **k)

def EncDownload(url):
	try:
		cj = CookieJar()
		opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
		opener.addheaders = [('User-Agent', UA_HML)]
		con = opener.open(url, timeout=TIMEOUT)
		data = con.read()
	except (urllib2.HTTPError, urllib2.URLError, socket.timeout) as error:
		log(error)
		return False

	# meta-redirect
	match = re.search(r'(?i)<meta http-equiv=.refresh[^>]*?url=(http.*?)["\']', data)
	if match:
		new_url = match.groups()[0]
		log('redirect: %s' % new_url)
		return EncDownload(new_url)

	# encoding
	if con.headers.getparam('charset'):
		log('header')
		enc = con.headers.getparam('charset')
	else:
		match = re.search('charset=["\']?([0-9a-zA-Z-]+)', data)
		if match:
			log('meta.re')
			enc = match.groups()[0]
		else:
			log('chardet')
			enc = chardet.detect(data)['encoding']

	log(enc)
	return (data.decode(enc, 'replace'), con.geturl())

def Fill(rss, cache, feedurl="/", fast=False):
	""" Returns True when it has done its best """

	item = XMLMap(rss, ITEM_MAP, True)
	log(item.link)

	if 'link' not in item:
		log('no link')
		return True

	# feedburner
	if '{http://rssnamespace.org/feedburner/ext/1.0}origLink' in item:
		item.link = item['{http://rssnamespace.org/feedburner/ext/1.0}origLink']
		log(item.link)

	# feedsportal
	match = re.search('/([0-9a-zA-Z]{20,})/story01.htm$', item.link)
	if match:
		url = match.groups()[0].split('0')
		t = {'A':'0', 'B':'.', 'C':'/', 'D':'?', 'E':'-', 'I':'_', 'L':'http://', 'S':'www.', 'N':'.com', 'O':'.co.uk'}
		item.link = "".join([(t[s[0]] if s[0] in t else "=") + s[1:] for s in url[1:]])
		log(item.link)

	# reddit
	if urlparse.urlparse(item.link).netloc == 'www.reddit.com':
		match = lxml.html.fromstring(item.desc).xpath('//a[text()="[link]"]/@href')
		if len(match):
			item.link = match[0]
			log(item.link)

	# check relative urls
	if urlparse.urlparse(item.link).netloc is '':
		item.link = urlparse.urljoin(feedurl, item.link)

	# check unwanted uppercase title
	if 'title' in item:
		if len(item.title) > 20 and item.title.isupper():
			item.title = item.title.title()

	# content already provided?
	if 'content' in item and 'desc' in item:
		len_content = lenHTML(item.content)
		len_desc = lenHTML(item.desc)
		log('content: %s vs %s' % (len_content, len_desc))
		if len_content > 5*len_desc:
			log('provided')
			return True

	# check cache and previous errors
	if item.link in cache:
		content = cache.get(item.link)
		match = re.search(r'^error-([a-z]{2,10})$', content)
		if match:
			if cache.isYoungerThan(DELAY*60):
				log('cached error: %s' % match.groups()[0])
				return True
			else:
				log('old error')
		else:
			log('cached')
			item.content = cache.get(item.link)
			return True

	# super-fast mode
	if fast:
		log('skipped')
		return False

	# download
	ddl = EncDownload(item.link.encode('utf-8'))

	if ddl is False:
		log('http error')
		cache.set(item.link, 'error-http')
		return True

	data, url = ddl

	out = readability.Document(data, url=url).summary(True)
	if 'desc' not in item or lenHTML(out) > lenHTML(item.desc):
		item.content = out
		cache.set(item.link, out)
	else:
		log('not bigger enough')
		cache.set(item.link, 'error-length')
		return True

	return True

def Gather(url, cachePath, mode='feed'):
	cache = Cache(cachePath, url)

	# fetch feed
	if cache.isYoungerThan(DELAY*60) and url in cache:
		log('xml cached')
		xml = cache.get(url)
	else:
		try:
			req = urllib2.Request(url)
			req.add_unredirected_header('User-Agent', UA_RSS)
			xml = urllib2.urlopen(req).read()
			cache.set(url, xml)
		except (urllib2.HTTPError, urllib2.URLError):
			return False

	xml = cleanXML(xml)
	rss = lxml.objectify.fromstring(xml)
	root = rss.channel if hasattr(rss, 'channel') else rss
	root = XMLMap(root, RSS_MAP)
	size = len(root.item)

	# set
	startTime = time.time()
	for i, item in enumerate(root.item):
		if mode == 'progress':
			if MAX_ITEM == 0:
				print "%s/%s" % (i+1, size)
			else:
				print "%s/%s" % (i+1, min(MAX_ITEM, size))
			sys.stdout.flush()

		if i+1 > LIM_ITEM > 0:
			item.getparent().remove(item)
		elif time.time() - startTime > MAX_TIME >= 0 or i+1 > MAX_ITEM > 0:
			if Fill(item, cache, url, True) is False:
				item.getparent().remove(item)
		else:
			Fill(item, cache, url)

	log(len(root.item))

	return root.tostring(xml_declaration=True, encoding='UTF-8')

if __name__ == "__main__":
	url, options = parseOptions(OPTIONS)

	if 'REQUEST_URI' in os.environ:
		print 'Status: 200'

		if options == 'progress':
			print 'Content-Type: application/octet-stream'
		else:
			print 'Content-Type: text/xml'
		print

		cache = os.getcwd() + '/cache'
		log(url)
	else:
		cache =	os.path.expanduser('~') + '/.cache/morss'

	if url is None:
		print "Please provide url."
		sys.exit(1)

	if options == 'progress':
		MAX_TIME = -1
	if options == 'cache':
		MAX_TIME = 0

	RSS = Gather(url, cache, options)

	if RSS is not False and options != 'progress':
		if 'REQUEST_URI' in os.environ or not os.getenv('DEBUG', False):
			print RSS

	if RSS is False and options != 'progress':
		print "Error fetching feed."

	log('done')
