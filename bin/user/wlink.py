#!/usr/bin/env python
#
# Copyright 2014 Matthew Wall
# Copyright 2019 Scott Shambarger

"""weewx driver for WeatherLink
   Download weather data from weatherlink.com for use in weewx

To use this driver:

1) copy this file to the weewx user directory, eg:

   cp wlink.py {weewx root}/user/

2) (optional) install pytz python module to correctly handle station timezone

   pip install pytz

3) configure weewx.conf

   wee_config --reconfigure --driver=user.wlink

4) test configuraion

   wee_device --info
"""

from __future__ import print_function
from six.moves import http_client
import six
import socket
import struct
import sys
import time
import calendar
import email.utils
import datetime
from six.moves import urllib
import json

import weewx
import weewx.drivers
import weewx.engine
from weeutil.weeutil import to_int

DRIVER_NAME = "WeatherLink"
DRIVER_VERSION = "0.17"

def loader(config_dict, engine):
    return WeatherLinkService(engine, config_dict)

def configurator_loader(config_dict):  # @UnusedVariable
    return WeatherLinkConfigurator()

def confeditor_loader():
    return WeatherLinkConfEditor()

DRIVER_CONSOLE_DEBUG = False

if weewx.__version__ < "3":
    raise weewx.UnsupportedFeature("weewx 3 is required, found %s"
                                   % weewx.__version__)

try:
    # Test for new-style weewx logging by trying to import weeutil.logger
    import weeutil.logger
    import logging
    log = logging.getLogger(__name__)

    def loginit():
        return

    def _logdbg(msg):
        log.debug(msg)

    def _loginf(msg):
        log.info(msg)

    def _logerr(msg):
        log.error(msg)

except ImportError:
    # Old-style weewx logging
    import syslog

    def loginit():
        syslog.openlog('wlink', syslog.LOG_PID | syslog.LOG_CONS)
        syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))

    def _logmsg(level, msg):
        syslog.syslog(level, 'wlink: %s' % msg)

    def _logdbg(msg):
        _logmsg(syslog.LOG_DEBUG, msg)

    def _loginf(msg):
        _logmsg(syslog.LOG_INFO, msg)

    def _logerr(msg):
        _logmsg(syslog.LOG_ERR, msg)

def logdbg(msg):
    if DRIVER_CONSOLE_DEBUG:
        print('debug: %s' % msg)
    else:
        _logdbg(msg)

def loginf(msg):
    if DRIVER_CONSOLE_DEBUG:
        print(msg)
    else:
        _loginf(msg)

def logerr(msg):
    if DRIVER_CONSOLE_DEBUG:
        print('err: %s' % msg)
    else:
        _logerr(msg)

# =============================================================================
#                      Decoding routines
# =============================================================================

def _big_val(v):
    return float(v) if v != 0x7fff else None

def _big_val10(v):
    return float(v)/10.0 if v != 0x7fff else None

def _val100(v):
    return float(v)/100.0

def _val1000(v):
    return float(v)/1000.0

def _val1000Zero(v):
    return float(v)/1000.0 if v != 0 else None

def _little_val(v):
    return float(v) if v != 0x00ff else None

def _little_val10(v):
    return float(v)/10.0 if v != 0x00ff else None

def _little_temp(v):
    return float(v-90) if v != 0x00ff else None

def _null(v):
    return v

def _null_float(v):
    return float(v)

def _windDir(v):
    return float(v) * 22.5 if v!= 0x00ff else None

# Rain bucket type "1", a 0.2 mm bucket
def _bucket_1(v):
    return float(v)*0.00787401575

# Rain bucket type "2", a 0.1 mm bucket
def _bucket_2(v):
    return float(v)*0.00393700787

class WeatherLink(weewx.drivers.AbstractDevice):
    """weewx driver to download data from weatherlink.com.

    Retrieves station configuration from the WeatherLink Station Status API.

    Downloads archive records from the Davis "Web Download" protocol.

    Parse 'loop' packets from the WeatherLink Current Conditions API.

    """

    rain_bucket_dict = {0:'0.01 inches', 1:'0.2 mm', 2:'0.1 mm'}

    def __init__(self, **stn_dict):
        loginf("version is %s" % DRIVER_VERSION)
        self._username = stn_dict['username']
        self._password = stn_dict['password']
        self._apitoken = stn_dict.get('apitoken')
        self._debug = stn_dict.get('debug')
        self._setup_rain_bucket(to_int(stn_dict.get('rain_bucket_type', -1)))
        self._archive_interval = to_int(stn_dict.get('archive_interval', 0))
        if self._archive_interval > 0:
            loginf("archive interval is %d seconds" % self._archive_interval)
        self._setup_tz(stn_dict.get('station_tz'),
                       stn_dict.get('station_dst', 1))
        self._poll_interval = to_int(stn_dict.get('poll_interval', 60))
        loginf("loop polling interval is %d secs" % self._poll_interval)
        self._max_tries = to_int(stn_dict.get('max_tries', 5))
        self._retry_wait = to_int(stn_dict.get('retry_wait', 30))
        self._setup_extra_sensors(stn_dict.get("extra_sensors", ""))
        # state values
        self._monthRain = None
        self._status_cache = None

    @property
    def hardware_name(self):
        """weewx api."""
        return "WeatherLink"

    @property
    def archive_interval(self):
        """weewx api.  Time in seconds between archive intervals."""
        if self._archive_interval <= 0:
            self._check_station_status(False)
        if self._archive_interval <= 0:
            raise NotImplementedError("Failed to query 'archive_interval' "
                                      "from StationStatus")
        return self._archive_interval

    def genLoopPackets(self):
        """Main generator function that continuously returns loop packets

        weewx api to return live records.

        yields: a sequence of dictionaries containing the data
        """
        while True:
            packet = self.get_readings()
            if packet:
                # calc rain based on monthRain changes
                monthRain = packet.get('monthRain')
                if self._monthRain is None:
                    self._monthRain = monthRain
                elif monthRain is not None:
                    delta = monthRain - self._monthRain
                    self._monthRain = monthRain
                    # will be negative at start of month
                    if delta >= 0: packet['rain'] = delta
                yield packet
            time.sleep(self._poll_interval)

    def genArchiveRecords(self, since_ts):
        """A generator function to return archive packets from weatherlink.com

        weewx api to return archive records.

        since_ts: A timestamp. All data since (but not including) this
          time will be returned. Pass in None for all data

        yields: a sequence of dictionaries containing the data
        """
        if not self._check_station_status():
            return
        ts = self._epoch_to_timestamp(since_ts)
        data = self._get_url("http://weatherlink.com/webdl.php"
                             "?timestamp=%s&user=%s&pass=%s&action=data"
                             % (ts, self._username, self._password))
        if data is None:
            return
        logdbg("found %s archive records since %s"
               % ((len(data) / 52), since_ts))
        sidx = 0
        while sidx < len(data):
            record = self._unpack_archive_packet(data[sidx:sidx + 52])
            if record is not None:
                # remove unneeded sensors
                for sensor in self._archive_extra:
                    if sensor not in self._extra_sensors:
                        if sensor in record: del record[sensor]
                logdbg("yielding: %s" % record)
                yield record
            sidx += 52

    def _setup_tz(self, station_tz, station_dst):
        """Set _station_tzinfo based on station_tz and station_dst
        station_tz can be 'localtime' to use server timezone,
        an fixed-offset (in mins, eg '-480'), or a named timezone
        (eg 'US/Pacific').  If a named timezone is used and station_dst = 0,
        then a fixed-offset timezone based on the non-DST offset for the
        timezone is applied"""
        self._station_tz = station_tz
        if station_tz is None: return
        try:
            tzoffset = int(station_tz)
        except ValueError:
            tzoffset = None
        if tzoffset:
            # fixed-offset tzinfo
            tz = FixedOffsetTZ(tzoffset)
        elif station_tz == 'localtime':
            tz = None
        else:
            try:
                # named timezone requires pytz
                import pytz
                tz = pytz.timezone(station_tz)
                if station_dst == 0:
                    # create fixed-offset based on non-dst delta
                    dt = datetime.datetime(2019, 1, 1)
                    tzoffset = tz.utcoffset(dt) + tz.dst(dt)
                    tzmins = (tzoffset.days * 1440) + (tzoffset.seconds / 60)
                    tz = FixedOffsetTZ(tzmins)
            except ImportError:
                logerr("pytz required to setup named timezone '%s' "
                       "- falling back to 'localtime'" % station_tz)
                self._station_tz = 'localtime'
                tz = None
            except:
                logerr("station timezone '%s' not recognized by pytz"
                       % station_tz)
                logerr("Set station_tz to 'localtime', or OFFSET_MINS "
                       "if daylight savings is not used")
                self._station_tz = None
                raise weewx.UnsupportedFeature("Unable to use station_tz '%s'"
                                               % station_tz)
        self._station_tzinfo = tz
        loginf("station timezone is '%s'" % self._station_tz)

    def _setup_extra_sensors(self, extra_sensors):
        """Set _extra_sensors array based on extra_sensors
        extra_sensors can be "all", "none", or a space separated list
        """
        if extra_sensors is None or not extra_sensors.strip():
            self._extra_sensors = None
            return
        if extra_sensors.lower().strip() == 'none':
            self._extra_sensors = []
        elif extra_sensors.lower().strip() == 'all':
            self._extra_sensors = self._archive_extra
        else:
            self._extra_sensors = [ v for v in extra_sensors.split()
                                    if v in self._archive_extra ]
            extra_sensors = ' '.join(self._extra_sensors)
        loginf("extra sensors are: %s" % extra_sensors)

    def _setup_rain_bucket(self, rain_bucket_type):
        """Adjust _archive_map based on rain_bucket_type"""
        # if unset/invalid, retrieve from StationStatus
        if rain_bucket_type < 0:
            self._rain_bucket_type = -1
            return
        if rain_bucket_type == 0:
            self._archive_map['rain'] = self._archive_map['rainRate'] \
                = _val100
        elif rain_bucket_type == 1:
            self._archive_map['rain'] = self._archive_map['rainRate'] \
                = _bucket_1
        elif rain_bucket_type == 2:
            self._archive_map['rain'] = self._archive_map['rainRate'] \
                = _bucket_2
        else:
            raise weewx.ViolatedPrecondition("Unknown rain_bucket_type: %d" % rain_bucket_type)
        self._rain_bucket_type = rain_bucket_type
        loginf("rain bucket type is %d (%s)"
               % (rain_bucket_type, self.rain_bucket_dict[rain_bucket_type]))

    def _query_api(self, mode):
        """Fetches json values from WeatherLink API, returns parsed dict"""
        url = "https://api.weatherlink.com/v1/%s.json?user=%s&pass=%s" \
            % (mode, self._username, self._password)
        # apiToken really only required for accounts with 2+ stations
        if self._apitoken:
            url += "&apiToken=%s" % self._apitoken
        data = self._get_url(url)
        if data is None:
            return None
        if data == b"Invalid Request!":
            logerr('server returned "Invalid Request!" for %s api (incorrect password/apitoken?)' % mode)
            return None
        try:
            jdata = json.loads(data)
        except (TypeError, ValueError):
            logerr('failed to parse %s json values' % mode)
            return None
        if self._debug:
            logdbg('%s' % _json_pretty(jdata))
        return jdata

    def _check_station_status(self, set_extra=True):
        """Retrieve StationStatus from WeatherLink API, and set any
        missing config values.
        Returns True if config values are set, False if not."""
        # skip if all status values valid
        if (self._archive_interval > 0 and self._rain_bucket_type >= 0 and
            self._station_tz and
            (not set_extra or self._extra_sensors != None)):
            return True
        jdata = self.get_status()
        if jdata is None:
            return False

        if self._archive_interval <= 0:
            arc_int_mins = int(jdata.get("station_archive_interval", 0))
            if arc_int_mins <= 0:
                raise weewx.ViolatedPrecondition(
                    "station_archive_interval missing in StationStatus")
            self._archive_interval = arc_int_mins * 60
            loginf("archive interval is %d seconds" % self._archive_interval)

        if self._rain_bucket_type < 0:
            rain_collector = jdata.get("station_rain_collector")
            if not rain_collector:
                raise weewx.ViolatedPrecondition(
                    "station_rain_collector missing in StationStatus")
            if rain_collector == "0.2 mm":
                rain_bucket_type = 1
            elif rain_collector == "0.1 mm":
                rain_bucket_type = 2
            elif rain_collector == "0.01 inches":
                rain_bucket_type = 0
            else:
                raise weewx.ViolatedPrecondition(
                    "Unknown station_rain_collector in StationStatus: '%s'"
                    % rain_collector)
            self._setup_rain_bucket(rain_bucket_type)

        if not self._station_tz:
            station_tz = jdata.get("station_timezone")
            station_dst_yn = jdata.get("station_daylight_observed", "no")
            station_dst = 1
            if station_dst_yn == "no":
                station_dst = 0
            if not station_tz:
                station_offset = jdata.get("station_time_offset")
                if not station_offset:
                    raise weewx.ViolatedPrecondition(
                        "Unable to obtain station timezone from StationStatus")
                if station_dst:
                    raise weewx.ViolatedPrecondition(
                        "Unable to use station_time_offset from StationStatus "
                        "when station_daylight_observed='yes'")
                offset = int(station_offset.split(' ')[0])
                offset_mins = (abs(offset) / 100 * 60) + abs(offset) % 100
                if offset < 0:
                    offset_mins = -offset_mins
                station_tz = str(offset_mins)
            self._setup_tz(station_tz, station_dst)

        if set_extra and self._extra_sensors is None:
            # we need to query a LOOP packet to see available sensors
            packet = self.get_readings()
            if packet is None:
                return False
            sensor_list = [ v for v in list(packet.keys())
                            if v in self._archive_extra ]
            # add non-loop values that are in archive
            if 'radiation' in sensor_list:
                sensor_list += [ 'highRadiation', 'ET' ]
            if 'UV' in sensor_list:
                sensor_list += [ 'highUV' ]
            extra_sensors = ' '.join(sensor_list)
            if not extra_sensors:
                extra_sensors = 'none'
            self._setup_extra_sensors(extra_sensors)

        return True

    def _get_url(self, url):
        """Returns content from url, with retries on network failures."""
        for count in range(self._max_tries):
            try:
                if self._debug:
                    logdbg('get_url: %s' % url)
                response = urllib.request.urlopen(url)
                return response.read()
            except (urllib.error.URLError, socket.error,
                    http_client.BadStatusLine, http_client.IncompleteRead) as e:
                logerr('download failed attempt %d of %d: %s'
                       % (count + 1, self._max_tries, e))
                time.sleep(self._retry_wait)
        # network failures happen... don't throw exception as it kills engine
        logerr('download failed after %d tries' % self._max_tries)
        return None

    def _archive_datetime(self, datestamp, timestamp):
        """Returns the epoch time of the archive packet, adjusted
        for the station timezone."""
        if datestamp == 0xffff or timestamp == 0xffff:
            return None
        try:
            time_tuple = (2000 + ((0xfe00 & datestamp) >> 9), # year
                          (0x01e0 & datestamp) >> 5,          # month
                          (0x001f & datestamp),               # day
                          timestamp // 100,                   # hour
                          timestamp % 100,                    # minute
                          0)                                  # second
            if self._debug:
                logdbg("archive timestamp: %s" % repr(time_tuple))
            if self._station_tz == 'localtime':
                ts = int(time.mktime(time_tuple + (0, 0, -1)))
            else:
                dt = datetime.datetime(*time_tuple)
                # ambiguous dates are converted with dst off...
                dz = self._station_tzinfo.localize(dt)
                ts = calendar.timegm(dz.utctimetuple())
        except (OverflowError, ValueError, TypeError):
            logerr("cannot make timestamp: ds=%s ts=%s, tz=%s"
                   % (datestamp, timestamp, self._station_tz))
            ts = None
        return ts

    # adapted from the implementation in the vantage driver
    def _unpack_archive_packet(self, raw_record_string):
        """Unpack a binary archive packet"""
        packet_type = six.indexbytes(raw_record_string, 42)
        if packet_type == 0xff:
            archive_format = _rec_fmt_A
            data_types = _rec_types_A
        elif packet_type == 0x00:
            archive_format = _rec_fmt_B
            data_types = _rec_types_B
        else:
            logerr("skipping packet of unknown archive type 0x%x"
                   % packet_type);
            return None
        data_tuple = archive_format.unpack(raw_record_string)
        raw_record = dict(list(zip(data_types, data_tuple)))
        packet = {
            'dateTime': self._archive_datetime(raw_record['date_stamp'],
                                               raw_record['time_stamp']),
            'usUnits': weewx.US,
        }
        if packet['dateTime'] is None:
            return None
        for type_ in raw_record:
            func = self._archive_map.get(type_)
            if func:
                v = func(raw_record[type_])
                if v is not None:
                    packet[type_] = v
        packet['interval'] = self._archive_interval / 60
        return packet

    def _epoch_to_timestamp(self, epoch):
        """convert unix timestamp to davis timestamp in station's timezone"""
        if not epoch:
            return 0
        if self._station_tz == 'localtime':
            tt = time.localtime(epoch)
        else:
            dt = datetime.datetime.fromtimestamp(epoch, self._station_tzinfo)
            tt = dt.timetuple()
        ds = tt[2] + (tt[1] << 5) + ((tt[0] - 2000) << 9)
        ts = tt[3] * 100 + tt[4]
        x = (ds << 16) | ts
        return x

    _archive_map = {
        'barometer'      : _val1000Zero,
        'inTemp'         : _big_val10,
        'outTemp'        : _big_val10,
        'highOutTemp'    : lambda v : float(v/10.0) if v != -32768 else None,
        'lowOutTemp'     : _big_val10,
        'inHumidity'     : _little_val,
        'outHumidity'    : _little_val,
        'windSpeed'      : _little_val,
        'windDir'        : _windDir,
        'windGust'       : _null_float,
        'windGustDir'    : _windDir,
        'rain'           : _val100,
        'rainRate'       : _val100,
        'ET'             : _val1000,
        'radiation'      : _big_val,
        'highRadiation'  : _big_val,
        'UV'             : _little_val10,
        'highUV'         : _little_val10,
        'extraTemp1'     : _little_temp,
        'extraTemp2'     : _little_temp,
        'extraTemp3'     : _little_temp,
        'soilTemp1'      : _little_temp,
        'soilTemp2'      : _little_temp,
        'soilTemp3'      : _little_temp,
        'soilTemp4'      : _little_temp,
        'leafTemp1'      : _little_temp,
        'leafTemp2'      : _little_temp,
        'extraHumid1'    : _little_val,
        'extraHumid2'    : _little_val,
        'soilMoist1'     : _little_val,
        'soilMoist2'     : _little_val,
        'soilMoist3'     : _little_val,
        'soilMoist4'     : _little_val,
        'leafWet1'       : _little_val,
        'leafWet2'       : _little_val,
        'leafWet3'       : _little_val,
        'leafWet4'       : _little_val,
        'forecastRule'   : _null,
        'readClosed'     : _null,
        'readOpened'     : _null,
    }

    _archive_extra = [
        'extraTemp1', 'extraTemp2', 'extraTemp3', 'soilTemp1', 'soilTemp2',
        'soilTemp3', 'soilTemp4', 'leafTemp1', 'leafTemp2', 'extraHumid1',
        'extraHumid2', 'soilMoist1', 'soilMoist2', 'soilMoist3', 'soilMoist4',
        'leafWet1', 'leafWet2', 'leafWet3', 'leafWet4', 'ET', 'radiation',
        'highRadiation', 'UV','highUV' ]

    def get_status(self):
        """Return StationStatus as dict
        Return None if status not available"""
        if self._status_cache is None:
            self._status_cache = self._query_api('StationStatus')
        return self._status_cache

    def get_readings(self):
        """Return LOOP packet"""
        if not self._check_station_status(False):
            return None
        jdata = self._query_api('NoaaExt')
        packet = None
        if jdata:
            packet = _parse_loop(jdata)
        return packet

class FixedOffsetTZ(datetime.tzinfo):
    """Fixed offset in minutes east from UTC."""

    def __init__(self, offset):
        self.__mins = offset
        self.__offset = datetime.timedelta(minutes = offset)
        self.__name = "%+03d%02d" % (offset / 60, offset % 60)

    def utcoffset(self, dt):
        return self.__offset

    def tzname(self, dt):
        return self.__name

    def dst(self, dt):
        return datetime.timedelta(0)

    def __repr__(self):
        return 'FixedOffsetTZ(%d)' % self.__mins

    def localize(self, dt, is_dst=False):
        '''Convert naive time to local time'''
        if dt.tzinfo is not None:
            raise ValueError('Not naive datetime (tzinfo is already set)')
        return dt.replace(tzinfo=self)


_rec_format_A =[
    ('date_stamp',  'H'), ('time_stamp',    'H'), ('outTemp',        'h'),
    ('highOutTemp', 'h'), ('lowOutTemp',    'h'), ('rain',           'H'),
    ('rainRate',    'H'), ('barometer',     'H'), ('radiation',      'H'),
    ('number_of_wind_samples', 'H'), ('inTemp', 'h'), ('inHumidity', 'B'),
    ('outHumidity', 'B'), ('windSpeed',     'B'), ('windGust',       'B'),
    ('windGustDir', 'B'), ('windDir',       'B'), ('UV',             'B'),
    ('ET',          'B'), ('invalid_data',  'B'), ('soilMoist1',     'B'),
    ('soilMoist2',  'B'), ('soilMoist3',    'B'), ('soilMoist4',     'B'),
    ('soilTemp1',   'B'), ('soilTemp2',     'B'), ('soilTemp3',      'B'),
    ('soilTemp4',   'B'), ('leafWet1',      'B'), ('leafWet2',       'B'),
    ('leafWet3',    'B'), ('leafWet4',      'B'), ('extraTemp1',     'B'),
    ('extraTemp2',  'B'), ('extraHumid1',   'B'), ('extraHumid2',    'B'),
    ('readClosed',  'H'), ('readOpened',    'H'), ('unused',         'B')
]

_rec_format_B = [
    ('date_stamp',  'H'), ('time_stamp',    'H'), ('outTemp',        'h'),
    ('highOutTemp', 'h'), ('lowOutTemp',    'h'), ('rain',           'H'),
    ('rainRate',    'H'), ('barometer',     'H'), ('radiation',      'H'),
    ('number_of_wind_samples', 'H'), ('inTemp','h'), ('inHumidity',  'B'),
    ('outHumidity', 'B'), ('windSpeed',     'B'), ('windGust',       'B'),
    ('windGustDir', 'B'), ('windDir',       'B'), ('UV',             'B'),
    ('ET',          'B'), ('highRadiation', 'H'), ('highUV',         'B'),
    ('forecastRule','B'), ('leafTemp1',     'B'), ('leafTemp2',      'B'),
    ('leafWet1',    'B'), ('leafWet2',      'B'), ('soilTemp1',      'B'),
    ('soilTemp2',   'B'), ('soilTemp3',     'B'), ('soilTemp4',      'B'),
    ('download_record_type', 'B'), ('extraHumid1', 'B'), ('extraHumid2', 'B'),
    ('extraTemp1',  'B'), ('extraTemp2',    'B'), ('extraTemp3',     'B'),
    ('soilMoist1',  'B'), ('soilMoist2',    'B'), ('soilMoist3',     'B'),
    ('soilMoist4',  'B')
]

_rec_types_A, _fmt_A = list(zip(*_rec_format_A))
_rec_types_B, _fmt_B = list(zip(*_rec_format_B))
_rec_fmt_A = struct.Struct('<' + ''.join(_fmt_A))
_rec_fmt_B = struct.Struct('<' + ''.join(_fmt_B))

# entries in current conditions
_cur_con_to_loop_float = {
    'pressure_in'        : 'barometer',
    'temp_f'             : 'outTemp',
    'wind_mph'           : 'windSpeed',
    'wind_degrees'       : 'windDir',
    'relative_humidity'  : 'outHumidity',
}
# entries in davis_current_observation
_cur_obs_to_loop_float = {
    'temp_in_f'             : 'inTemp',
    'relative_humidity_in'  : 'inHumidity',
    'wind_ten_min_avg_mph'  : 'windSpeed10',
    'temp_extra_1'          : 'extraTemp1',
    'temp_extra_2'          : 'extraTemp2',
    'temp_extra_3'          : 'extraTemp3',
    'temp_extra_4'          : 'extraTemp4',
    'temp_extra_5'          : 'extraTemp5',
    'temp_extra_6'          : 'extraTemp6',
    'temp_extra_7'          : 'extraTemp7',
    'temp_soil_1'           : 'soilTemp1',
    'temp_soil_2'           : 'soilTemp2',
    'temp_soil_3'           : 'soilTemp3',
    'temp_soil_4'           : 'soilTemp4',
    'temp_leaf_1'           : 'leafTemp1',
    'temp_leaf_2'           : 'leafTemp2',
    'temp_leaf_3'           : 'leafTemp3',
    'temp_leaf_4'           : 'leafTemp4',
    'relative_humidity_1'   : 'extraHumid1',
    'relative_humidity_2'   : 'extraHumid2',
    'relative_humidity_3'   : 'extraHumid3',
    'relative_humidity_4'   : 'extraHumid4',
    'relative_humidity_5'   : 'extraHumid5',
    'relative_humidity_6'   : 'extraHumid6',
    'relative_humidity_7'   : 'extraHumid7',
    'rain_rate_in_per_hr'   : 'rainRate',
    'uv_index'              : 'UV',
    'solar_radiation'       : 'radiation',
    'rain_storm_in'         : 'stormRain',
    'rain_day_in'           : 'dayRain',
    'rain_month_in'         : 'monthRain',
    'rain_year_in'          : 'yearRain',
    'et_day'                : 'dayET',
    'et_month'              : 'monthET',
    'et_year'               : 'yearET',
    'soil_moisture_1'       : 'soilMoist1',
    'soil_moisture_2'       : 'soilMoist2',
    'soil_moisture_3'       : 'soilMoist3',
    'soil_moisture_4'       : 'soilMoist4',
    'leaf_wetness_1'        : 'leafWet1',
    'leaf_wetness_2'        : 'leafWet2',
    'leaf_wetness_3'        : 'leafWet3',
    'leaf_wetness_4'        : 'leafWet4',
    'wind_ten_min_gust_mph' : 'windGust',
}

def _parse_date(dstr, fmt, tz_offset):
    """Parse dstr using strptime fmt, returns unix timestamp adjusted for
    tz_offset"""
    try:
        ts = calendar.timegm(time.strptime(dstr, fmt))
        return ts - tz_offset
    except (ValueError, TypeError):
        return None

def _parse_rfc822(dstr):
    """Parses rfc822 date string, returns timestamp and timezone offset"""
    try:
        tt = email.utils.parsedate_tz(dstr)
        return calendar.timegm(tt[:6]) - int(tt[9]), int(tt[9])
    except (ValueError, TypeError):
        return None, 0

def _parse_time(tstr, ts, tz_offset):
    """Parses HH:MM(am/pm) string, and then offsets it from start of day
    based on timestamp and tz_offset"""
    try:
        tlist = tstr[:-2].split(':')
        hour = int(tlist[0])
        if hour == 12: hour = 0
        if tstr[-2:] == "pm": hour += 12
        secs = hour * 3600 + int(tlist[1]) * 60
        tt = time.gmtime(ts+tz_offset)
        utc_start = calendar.timegm((tt.tm_year, tt.tm_mon, tt.tm_mday,
                                     0, 0, 0))
        return utc_start - tz_offset + secs
    except (ValueError, TypeError):
        return None

def _parse_loop(jdata):
    """Parse loop values from WeatherLink API current conditions"""
    ts, tz_offset = _parse_rfc822(jdata.get('observation_time_rfc822'))
    if ts is None: ts = int(time.time() + 0.5)
    packet = {
        'dateTime': ts,
        'usUnits': weewx.US, # type 2 is US imperial
    }
    packet.update(_map_floats(jdata, _cur_con_to_loop_float))
    obs = jdata.get('davis_current_observation', {})
    packet.update(_map_floats(obs, _cur_obs_to_loop_float))
    storm_start = _parse_date(obs.get('rain_storm_start_date'), "%m/%d/%Y",
                              tz_offset)
    if storm_start is not None: packet['stormStart'] = storm_start
    sunrise = _parse_time(obs.get('sunrise'), ts, tz_offset)
    if sunrise is not None: packet['sunrise'] = sunrise
    sunset = _parse_time(obs.get('sunset'), ts, tz_offset)
    if sunset is not None: packet['sunset'] = sunset
    return packet

def _map_floats(jdata, mapping):
    """Map keys to new names and convert to float"""
    values = {}
    for k1,k2 in list(mapping.items()):
        v = jdata.get(k1)
        if v:
            values[k2] = float(v)
    return values

def _json_pretty(jdata):
    """Return dict as indent-formatted json"""
    return json.dumps(jdata, indent=4, skipkeys=True)

#==============================================================================
#                      class WeatherLinkService
#==============================================================================

# This class uses multiple inheritance:

class WeatherLinkService(WeatherLink, weewx.engine.StdService):
    """Weewx service for WeatherLink."""

    def __init__(self, engine, config_dict):
        WeatherLink.__init__(self, **config_dict[DRIVER_NAME])
        weewx.engine.StdService.__init__(self, engine, config_dict)

        self.bind(weewx.STARTUP, self._init_state)
        self.bind(weewx.NEW_LOOP_PACKET,    self._new_loop_packet)
        self.bind(weewx.END_ARCHIVE_PERIOD, self._init_state)

    def _init_state(self, event):  # @UnusedVariable
        self._max_loop_gust = 0.0
        self._max_loop_gustdir = None

    def _new_loop_packet(self, event):
        """Calculate the max gust seen since the last archive record."""

        # Calculate the max gust seen since the start of this archive record
        # and put it in the packet.
        windSpeed = event.packet.get('windSpeed')
        windDir   = event.packet.get('windDir')
        if windSpeed is not None and windSpeed > self._max_loop_gust:
            self._max_loop_gust = windSpeed
            self._max_loop_gustdir = windDir
        event.packet['windGust'] = self._max_loop_gust
        event.packet['windGustDir'] = self._max_loop_gustdir

#==============================================================================
#                      Class WeatherLinkConfigurator
#==============================================================================

class WeatherLinkConfigurator(weewx.drivers.AbstractConfigurator):
    @property
    def description(self):
        return "Queries weatherlink.com for StationStatus"

    @property
    def usage(self):
        return """%prog [config_file] [--help] [--info]"""

    def add_options(self, parser):
        super(WeatherLinkConfigurator, self).add_options(parser)
        self._add_options(parser)

    @staticmethod
    def _add_options(parser):
        parser.add_option("--info", dest="info", action="store_true",
                          help="print device info")
        parser.add_option("--current", dest="current", action="store_true",
                          help="get the current weather conditions")
        parser.add_option("--format", dest="format",
                          type=str, metavar="FORMAT", default='table',
                          help="formats include: table, dict")

    def do_options(self, options, parser, config_dict, prompt):  # @UnusedVariable
        self._do_options(options, parser, **config_dict[DRIVER_NAME])

    @staticmethod
    def _do_options(options, parser, **stn_dict):
        options.format = options.format.lower()
        if (options.format != 'table' and
            options.format != 'dict'):
            parser.error("Unknown format '%s'.  Known formats include 'table' and 'dict'." % options.format)
        if not (options.info or options.current):
            return False
        station = WeatherLink(**stn_dict)
        if options.info is not None:
            WeatherLinkConfigurator.show_info(station, fmt=options.format)
        elif options.current is not None:
            WeatherLinkConfigurator.show_current(station, fmt=options.format)
        return True

    @staticmethod
    def _print_data(data, fmt):
        if fmt == 'table':
            WeatherLinkConfigurator._print_table(data)
        else:
            print("%s" % data)

    @staticmethod
    def _print_table(data):
        for key in sorted(data):
            print("%s: %s" % (key.rjust(16), data[key]))

    @staticmethod
    def show_info(station, fmt='dict'):
        """Display StationStatus"""

        print("Querying...")
        data = station.get_status()
        if data is None:
            print("Unable to retrieve station status")
            return
        if fmt == 'dict':
            print("%s" % data)
            return
        print("""WeatherLink Station Status:

    STATION NAME:                   %s
    CONSOLE TYPE:                   %s
    DEVICE ID:                      %s
    HARDWARE VERSION:               %s
    FIRMWARE VERSION:               %s

    CONSOLE SETTINGS:
      Archive interval:             %d (seconds)
      Altitude:                     %s (feet)
      Latitude:                     %s
      Longitude:                    %s
      Rain bucket type:             %s
      Timezone:                     %s
      Daylight Savings Observed:    %s
      Time Offset:                  %s
        """ % (data.get('station_name'),
               data.get('station_type'),
               data.get('station_did'),
               data.get('station_hardware'),
               data.get('station_firmware'),
               (int(data.get('station_archive_interval', 0)) * 60),
               data.get('station_elevation'),
               data.get('station_latitude'),
               data.get('station_longitude'),
               data.get('station_rain_collector'),
               data.get('station_timezone'),
               data.get('station_daylight_observed'),
               data.get('station_time_offset')))

    @staticmethod
    def show_current(station, fmt='dict'):
        """Display LOOP packet"""
        print("Querying the current weather data...")
        data = station.get_readings()
        if data is None:
            print("Unable to retrieve current conditions")
            return
        WeatherLinkConfigurator._print_data(data, fmt)

# =============================================================================
#                      Class WeatherLinkConfEditor
# =============================================================================

class WeatherLinkConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[WeatherLink]
    # This section is for retrieving data from weatherlink.com.

    # Device ID (can be found on the WeatherLink Device Info page)
    username = 001D0A00DE6A

    # WeatherLink password (weatherlink.com account password)
    password = DEMO

    # The following are optional:

    # apiToken, if needed (not required as of Aug 2019)
    apitoken = ""

    # extra debug logging
    #debug = None

    ######################################################
    # These values are retrieved from the StationStatus
    # API, and should only be set if those values are
    # incorrect.
    ######################################################

    # Rain bucket size: 0 = 0.01in, 1 = 0.2mm, 2 = 0.1mm, -1 = <FROM API>
    #rain_bucket_type = -1

    # Station archive interval (secs, 0 = <FROM API>)
    #archive_interval = 0

    # Station timezone.  This can be named (eg 'US/Pacific'),
    # an fixed offset (eg '-480'), or 'localtime' to use the
    # servers timezone.
    # NOTE: named timezones, if set here or retrieved from the API
    #       require the pytz python module, and if not present,
    #       will fallback to 'localtime' to be compatible with
    #       wlink's original behavior.
    #station_tz = <FROM API>

    # Station uses daylight savings on dates (1 = yes, 0 = no)
    # NOTE: this is only used if station uses a named timezone.
    #station_dst = 1

    # Station extra sensors - Limit extra sensor values in archive packets
    # 'none' - No extra sensors (including UV/radiation)
    # 'all' - Any valid extra sensor values included in archive data
    # space separated keywords: eg "soilMoist1 soilTemp3 leafWet4 extraTemp2"
    #   (NOTE: case-sensive!)
    #extra_sensors = <DERIVED FROM LOOP VALUES>

    ######################################################
    # The rest of this section rarely needs any attention.
    # You can safely leave it "as is."
    ######################################################

    # LOOP packet poll interval (secs)
    #poll_interval = 60

    # Network connection retries (secs)
    #max_tries = 5

    # Delays between connect retries (secs)
    #retry_wait = 30

    # The driver to use:
    driver = user.wlink
"""

    def prompt_for_settings(self):
        settings = dict()
        print("Enter the Device ID")
        print(" - This can be found on the WeatherLink Device Info page")
        settings['username'] = self._prompt('username')
        print("Enter the weatherlink.com account password")
        settings['password'] = self._prompt('password')
        print("Enter the weatherlink.com account apiToken (optional)")
        print(" - This can be found on the WeatherLink Account Information page")
        settings['apitoken'] = self._prompt('apitoken')
        return settings

# Invoke this as follows from the weewx user directory:
#
# PYTHONPATH=../ python -m user.wlink

if __name__ == "__main__":
    import optparse

    usage = """%prog [options] [--help]"""

    parser = optparse.OptionParser(usage=usage)
    # values for driver
    parser.add_option('--version', action='store_true',
                      help='Display driver version')
    parser.add_option('--username', dest='username', default='001D0A00DE6A',
                      help='Device ID')
    parser.add_option('--password', dest='password', default='DEMO',
                      help='WeatherLink account password')
    parser.add_option('--apitoken', dest='apitoken',
                      help='WeatherLink account apiToken')
    parser.add_option('--debug', dest='debug', action='store_true',
                      help='print debug output to console')
    parser.add_option('--rain-bucket-type', dest='rain_bucket_type',
                      default=-1, type='int', help='rain bucket type (0,1,2)')
    parser.add_option('--archive-interval', dest='archive_interval',
                      type='int', default=0, help='archive interval (secs)')
    parser.add_option('--station-tz', dest='station_tz',
                      help='station timezone')
    parser.add_option('--station-dst', dest='station_dst', type='int',
                      default=1, help='station uses daylight savings')
    parser.add_option('--poll-interval', dest='poll_interval', default=2,
                      type='float', help='loop poll interval (secs)')
    parser.add_option('--extra-sensors', dest='extra_sensors', default="",
                      help='extra sensors to include in archive record')
    # configurator options
    WeatherLinkConfigurator._add_options(parser)
    # options for testing
    parser.add_option('--test-parser', dest='test_parser', action='store_true',
                      help='test the parser')
    parser.add_option('--filename', dest='filename', default='testfile.json',
                      help='name of file for test-parser')
    parser.add_option('--test-driver', dest='test_driver', action='store_true',
                      help='test the driver (LOOP, --since-ts for archive)')
    parser.add_option('--since-ts', dest='since_ts', type='int', default=-1,
                      help='query archive records since this epoch timestamp')
    parser.add_option('--record-limit', dest='record_limit', default=0,
                      type='int', help='limit # of driver records to display')
    (options, args) = parser.parse_args()

    if options.debug:
        DRIVER_CONSOLE_DEBUG = True
    else:
        loginit()

    if options.version:
        print("Weatherlink driver version %s" % DRIVER_VERSION)
        exit(0)

    if options.username == '001D0A00DE6A':
        print("WARNING: Using DEMO user!")

    if WeatherLinkConfigurator._do_options(options, parser, **(vars(options))):
        exit(0)

    if options.test_parser:
        data = []
        with open(options.filename) as f:
            for line in f:
                data.append(line)
        jdata = json.loads(''.join(data))
        if options.debug:
            print(("%s: " % options.filename) + _json_pretty(jdata))
        packet = _parse_loop(jdata)
        print("parsed loop: " + _json_pretty(packet))
    elif options.test_driver:
        import weeutil.weeutil
        station = WeatherLink(**(vars(options)))
        if options.since_ts >= 0:
            func = station.genArchiveRecords
            args = [options.since_ts]
        else:
            func = station.genLoopPackets
            args=[]
        n = 0
        for p in func(*args):
            if station._station_tz == 'localtime':
                print(weeutil.weeutil.timestamp_to_string(p['dateTime']) +
                      ' ' + _json_pretty(p))
            else:
                dt = datetime.datetime.fromtimestamp(int(p['dateTime']),
                                                     station._station_tzinfo)
                print(dt.strftime("%Y-%m-%d %H:%M:%S %Z")
                      + (" (%d) " % p['dateTime']) + _json_pretty(p))
            n += 1
            if options.record_limit and n >= options.record_limit:
                break
