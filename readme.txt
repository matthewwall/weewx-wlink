wlink - weewx driver that gets data from a weatherlink web site
Copyright 2014 Matthew Wall

Install using wee_extension:

1) Install from tarball

wee_extension --install weewx-wlink.tgz

2) Reconfigure with wee_config (will add config to weewx.conf)

wee_config --reconfigure

  * Select WeatherLink (user.wlink) as driver
  * Supply Device ID and weatherlink.com account password when prompted

3) Start weewx

---

Test that API query is working:

wee_device --info

Retrieve current conditions:

wee_device --current

(Advanced) Test driver directly:

cd ${WEEWX_INSTALL_DIR}/user
PYTHONPATH=../ python wlink.py --help

Retrieve and display 10 records from two hours ago (not saved to archive):

PYTHONPATH=../ python wlink.py --test-driver --username={DID} \
  --password={secret} --record-limit=10 --since-ts=$(($(date +%s) - 7200))

---

ALTERNATIVE: Manual installation instructions:

1) expand the tarball:

cd /var/tmp
tar xvfz ~/Downloads/weewx-wlink.tgz

2) copy files to the weewx user directory:

cp /var/tmp/weewx-wlink/bin/user/wlink.py /home/weewx/bin/user

3) modify weewx.conf

[Station]
    station_type = WeatherLink

[WeatherLink]
    driver = user.wlink
    username = USERNAME
    password = PASSWORD

4) start weewx

sudo /etc/init.d/weewx start
