from collections import OrderedDict
from distutils.version import LooseVersion
from glob import glob
import json
import os
import logging
from zipfile import ZipFile, BadZipfile
import re
from bs4 import BeautifulSoup
from mechanize import Browser
from ips_vagrant.common import http_session
from ips_vagrant.scrapers.errors import HtmlParserError


class IpsManager(object):
    """
    IPS Versions Manager
    """
    # noinspection PyShadowingBuiltins
    def __init__(self, ctx, license):
        """
        @type   ctx:        ips_vagrant.cli.Context
        @type   license:    ips_vagrant.scraper.licenses.LicenseMeta
        """
        self.ctx = ctx
        self.session = http_session(ctx.cookiejar)
        self._license = license

        self.path = os.path.join(self.ctx.config.get('Paths', 'Data'), 'versions', 'ips')
        self.versions = OrderedDict()

        self._populate_local()
        self._populate_latest()
        self._sort()
        self.log = logging.getLogger('ipsv.scraper.version')

    def _sort(self):
        """
        Sort versions by their version number
        """
        self.versions = OrderedDict(sorted(self.versions.items(), key=lambda v: v[0]))

    def _populate_local(self):
        """
        Populate version data for local archives
        """
        archives = glob(os.path.join(self.path, '*.zip'))
        for archive in archives:
            try:
                version = self._read_zip(archive)
                self.versions[version] = IpsMeta(self, version, filepath=archive)
            except BadZipfile as e:
                self.log.warn('Unreadable zip archive in IPS versions directory (%s): %s', e.message, archive)

    def _populate_latest(self):
        """
        Popular version data for the latest release available for download
        """
        # Submit a request to the client area
        response = self.session.get(self._license.license_url)
        self.log.debug('Response code: %s', response.status_code)
        response.raise_for_status()

        # Load our license page
        soup = BeautifulSoup(response.text, "html.parser")
        script_tpl = soup.find('script', id='download_form')
        form = BeautifulSoup(script_tpl.text, "html.parser").find('form')

        # Parse the response for a download link to the latest IPS release
        version = LooseVersion(form.find('label', {'for': 'version_latest'}).text).version
        self.log.info('Latest IPS version: %s', version)
        url = form.get('action')

        # If we have a cache for this version, just add our url to it
        if version in self.versions:
            self.versions[version].url = url
            return

        self.versions[version] = IpsMeta(self, version, request=('post', url, {'version': 'latest'}))

    def _read_zip(self, filepath):
        """
        Read an IPS installation zipfile and return the core version number
        @type   filepath:   str
        @rtype: tuple
        """
        with ZipFile(filepath) as zip:
            namelist = zip.namelist()
            if re.match(r'^ips_\w{5}\/?$', namelist[0]):
                self.log.debug('Setup directory matched: %s', namelist[0])
            else:
                self.log.error('No setup directory matched')
                raise BadZipfile('Unrecognized setup file format')

            versions_path = os.path.join(namelist[0], 'applications/core/data/versions.json')
            if versions_path not in namelist:
                raise BadZipfile('Missing versions.json file')
            versions = json.loads(zip.read(versions_path))
            version = versions[-1]

            self.log.debug('Version matched: ', version)
            return LooseVersion(version).version

    def get(self, version, use_cache=True):
        """
        Get the filepath to the specified version (downloading it in the process if necessary)
        @type   version:    IpsMeta
        @param  use_cache:  Use cached version downloads if available
        @type   use_cache:  bool
        @rtype: str
        """
        if version.filepath:
            if use_cache:
                return version.filepath
            else:
                self.log.info('Ignoring cached version: %s', version.version)

        if not use_cache:
            self.log.info("We can't ignore the cache of a version that hasn't been downloaded yet")

        version.download()
        return version.filepath

    @property
    def latest(self):
        return next(reversed(self.versions))


class IpsMeta(object):
    """
    Version metadata container
    """
    def __init__(self, ips_manager, version, filepath=None, request=None):
        """
        @type   ips_manager:    IpsManager
        @type   version:        tuple
        @type   filepath:       str or None
        @type   request:        tuple or None (method, url, params)
        """
        self.ips_manager = ips_manager
        self.filepath = filepath
        self.version = version
        self.url = request
        self.log = logging.getLogger('ipsv.scraper.version')

        self.session = self.ips_manager.session
        self._browser = Browser()

    def download(self):
        """
        Download the latest IPS release
        @return:    Download file path
        @rtype:     str
        """
        # Submit a download request and test the response
        response = self.session.request(*self.url, stream=True)
        if response.status_code != 200:
            self.log.error('Download request failed: %d', response.status_code)
            raise HtmlParserError

        # If we're re-downloading this version, delete the old file
        if self.filepath and os.path.isfile(self.filepath):
            self.log.info('Removing old version download')
            os.remove(self.filepath)

        # Make sure our versions data directory exists
        if not os.path.isdir(os.path.join(self.ips_manager.path)):
            self.log.debug('Creating versions data directory')
            os.makedirs(self.ips_manager.path, 0o755)

        # Process our file download
        self.filepath = self.filepath or os.path.join(self.ips_manager.path, '{v}.zip'.format(v=self.version))
        with open(self.filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    f.flush()

        self.log.info('Version {v} successfully downloaded to {fn}'.format(v=self.version, fn=self.filepath))
        self.filepath = self.filepath