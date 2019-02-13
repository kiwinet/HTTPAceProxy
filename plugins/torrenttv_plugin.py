#-*- coding: utf-8 -*-
'''
Torrent-tv.ru Playlist Downloader Plugin
http://ip:port/torrenttv
'''

__author__ = 'AndreyPavlenko, Dorik1972'

import traceback
import gevent, requests
import logging, zlib
from urllib3.packages.six.moves.urllib.parse import urlparse, parse_qs, quote, unquote
from urllib3.packages.six import ensure_text, ensure_str
from PluginInterface import AceProxyPlugin
from PlaylistGenerator import PlaylistGenerator
import config.torrenttv as config
import config.picons.torrenttv as picons

class Torrenttv(AceProxyPlugin):

    # ttvplaylist handler is obsolete
    handlers = ('torrenttv', 'ttvplaylist')

    def __init__(self, AceConfig, AceProxy):
        self.logger = logging.getLogger('torrenttv_plugin')
        self.picons = self.channels = self.playlist = self.playlisttime = self.etag = self.last_modified = None
        self.headers = {'User-Agent': 'Magic Browser'}
        if config.updateevery: gevent.spawn(self.playlistTimedDownloader)

    def playlistTimedDownloader(self):
        while 1:
            self.Playlistparser()
            gevent.sleep(config.updateevery * 60)

    def Playlistparser(self):
        try:
           with requests.get(config.url, headers=self.headers, proxies=config.proxies, stream=False, timeout=30) as r:
              if r.encoding is None: r.encoding = 'utf-8'
              self.playlisttime = gevent.time.time()
              self.playlist = PlaylistGenerator(m3uchanneltemplate=config.m3uchanneltemplate)
              self.picons = picons.logomap
              self.channels = {}
              m = requests.auth.hashlib.md5()
              self.logger.info('Playlist %s downloaded' % config.url)
              pattern = requests.auth.re.compile(r',(?P<name>.+) \((?P<group>.+)\)[\r\n]+(?P<url>[^\r\n]+)?')
              for match in pattern.finditer(r.text, requests.auth.re.MULTILINE):
                 itemdict = match.groupdict()
                 name = itemdict.get('name', '')
                 if not 'logo' in itemdict: itemdict['logo'] = picons.logomap.get(name)
                 self.picons[name] = itemdict['logo']

                 url = itemdict['url']
                 if url.startswith(('acestream://', 'infohash://')) \
                       or (url.startswith(('http://','https://')) and url.endswith(('.acelive','.acestream','.acemedia'))):
                    self.channels[name] = url
                    itemdict['url'] = quote(ensure_str(name+'.ts'),'')

                 self.playlist.addItem(itemdict)
                 m.update(name.encode('utf-8'))

              self.etag = '"' + m.hexdigest() + '"'
              self.logger.debug('torrenttv.m3u playlist generated')

        except requests.exceptions.RequestException: self.logger.error("Can't download %s playlist!" % config.url); return False
        except: self.logger.error(traceback.format_exc()); return False

        return True

    def handle(self, connection, headers_only=False):
        play = False
        # 30 minutes cache
        if not self.playlist or (gevent.time.time() - self.playlisttime > 30 * 60):
           with requests.head(config.url, headers=self.headers, proxies=config.proxies, timeout=30) as r:
              try:
                 url_time = r.headers.get('last-modified')
                 if url_time:
                    url_time = gevent.time.mktime(gevent.time.strptime(url_time ,"%a, %d %b %Y %I:%M:%S %Z"))
                    if self.last_modified is None or url_time > self.last_modified:
                       if not self.Playlistparser(): connection.dieWithError(); return
                       self.last_modified = url_time
                 else:
                    if not self.Playlistparser(): connection.dieWithError(); return
              except requests.exceptions.RequestException:
                 self.logger.error("Playlist %$ not available !" % config.url)

        url = urlparse(connection.path)
        path = url.path[0:-1] if url.path.endswith('/') else url.path
        params = parse_qs(connection.query)

        if path.startswith('/%s/channel/' % connection.reqtype):
            name = path.rsplit('.', 1)
            if not name[1]:
                connection.dieWithError(404, 'Invalid path: %s' % unquote(path), logging.ERROR)
                return
            name = ensure_text(unquote(name[0].rsplit('/', 1)[1]))
            url = self.channels.get(name)
            if url is None:
                connection.dieWithError(404, 'Unknown channel: ' + name, logging.ERROR)
                return
            elif url.startswith('acestream://'):
                connection.path = '/content_id/%s/%s.ts' % (url.split('/')[2], name)
            elif url.startswith('infohash://'):
                connection.path = '/infohash/%s/%s.ts' % (url.split('/')[2], name)
            elif url.startswith(('http://', 'https://')) and url.endswith(('.acelive', '.acestream', '.acemedia')):
                connection.path = '/url/%s/%s.ts' % (quote(url,''), name)
            connection.splittedpath = connection.path.split('/')
            connection.reqtype = connection.splittedpath[1].lower()
            play = True
        elif self.etag == connection.headers.get('If-None-Match'):
            self.logger.debug('ETag matches - returning 304')
            connection.send_response(304)
            connection.send_header('Connection', 'close')
            connection.end_headers()
            return
        else:
            hostport = connection.headers['Host']
            path = '' if len(self.channels) == 0 else '/%s/channel' % connection.reqtype
            add_ts = True if path.endswith('/ts') else False
            exported = self.playlist.exportm3u(hostport=hostport, path=path, add_ts=add_ts, header=config.m3uheadertemplate, fmt=params.get('fmt', [''])[0])
            response_headers = { 'Content-Type': 'audio/mpegurl; charset=utf-8', 'Connection': 'close', 'Content-Length': len(exported),
                                 'Access-Control-Allow-Origin': '*', 'ETag': self.etag }
            try:
               h = connection.headers.get('Accept-Encoding').split(',')[0]
               compress_method = { 'zlib': zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS),
                                   'deflate': zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS),
                                   'gzip': zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16) }
               exported = compress_method[h].compress(exported) + compress_method[h].flush()
               response_headers['Content-Length'] = len(exported)
               response_headers['Content-Encoding'] = h
            except: pass

            connection.send_response(200)
            gevent.joinall([gevent.spawn(connection.send_header, k, v) for (k,v) in response_headers.items()])
            connection.end_headers()

        if play: connection.handleRequest(headers_only, name, self.picons.get(name), fmt=params.get('fmt', [''])[0])
        elif not headers_only:
            self.logger.debug('Exporting torrenttv.m3u playlist')
            connection.wfile.write(exported)
