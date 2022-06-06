# $Id: install.py 1783 2018-01-29 11:57:57Z mwall $
# installer for WeatherLink driver
# Copyright 2014 Matthew Wall

from setup import ExtensionInstaller

def loader():
    return WeatherLinkInstaller()

class WeatherLinkInstaller(ExtensionInstaller):
    def __init__(self):
        super(WeatherLinkInstaller, self).__init__(
            version="0.16",
            name='wlink',
            description='driver that collects data from weatherlink.com',
            author="Matthew Wall",
            author_email="mwall@users.sourceforge.net",
            files=[('bin/user', ['bin/user/wlink.py'])]
            )
