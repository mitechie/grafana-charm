#Overview

This charm provides the latest version of Grafana.

#Usage

    juju deploy grafana

#Development

Explicitly set `JUJU_REPOSITORY`:

    export JUJU_REPOSITORY=/path/to/charms
    mkdir -p $JUJU_REPOSITORY/layers

Branch code to

    $JUJU_REPOSITORY/layers/prometheus/

Modify

Assemble the charm:

    charm build

#Contact Information

Author: Alvaro Uria <alvaro.uria@canonical.com>
Report bugs at: http://bugs.launchpad.net/~canonical-bootstack/charms/trusty/bootstack-grafana/composer
Location: http://jujucharms.com/charms/trusty/bootstack-grafana
