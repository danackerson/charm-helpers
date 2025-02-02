# Copyright 2014-2016 Canonical Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#
# Copyright 2016 Canonical Ltd.
#
# Authors:
#  Openstack Charmers <
#

"""
Helpers for high availability.
"""

import hashlib
import json

import re

from charmhelpers.core.hookenv import (
    expected_related_units,
    log,
    relation_set,
    charm_name,
    config,
    status_set,
    DEBUG,
)

from charmhelpers.core.host import (
    lsb_release
)

from charmhelpers.contrib.openstack.ip import (
    resolve_address,
    is_ipv6,
)

from charmhelpers.contrib.network.ip import (
    get_iface_for_address,
    get_netmask_for_address,
)

from charmhelpers.contrib.hahelpers.cluster import (
    get_hacluster_config
)

JSON_ENCODE_OPTIONS = dict(
    sort_keys=True,
    allow_nan=False,
    indent=None,
    separators=(',', ':'),
)

VIP_GROUP_NAME = 'grp_{service}_vips'
DNSHA_GROUP_NAME = 'grp_{service}_hostnames'


class DNSHAException(Exception):
    """Raised when an error occurs setting up DNS HA
    """

    pass


def update_dns_ha_resource_params(resources, resource_params,
                                  relation_id=None,
                                  crm_ocf='ocf:maas:dns'):
    """ Configure DNS-HA resources based on provided configuration and
    update resource dictionaries for the HA relation.

    @param resources: Pointer to dictionary of resources.
                      Usually instantiated in ha_joined().
    @param resource_params: Pointer to dictionary of resource parameters.
                            Usually instantiated in ha_joined()
    @param relation_id: Relation ID of the ha relation
    @param crm_ocf: Corosync Open Cluster Framework resource agent to use for
                    DNS HA
    """
    _relation_data = {'resources': {}, 'resource_params': {}}
    update_hacluster_dns_ha(charm_name(),
                            _relation_data,
                            crm_ocf)
    resources.update(_relation_data['resources'])
    resource_params.update(_relation_data['resource_params'])
    relation_set(relation_id=relation_id, groups=_relation_data['groups'])


def assert_charm_supports_dns_ha():
    """Validate prerequisites for DNS HA
    The MAAS client is only available on Xenial or greater

    :raises DNSHAException: if release is < 16.04
    """
    if lsb_release().get('DISTRIB_RELEASE') < '16.04':
        msg = ('DNS HA is only supported on 16.04 and greater '
               'versions of Ubuntu.')
        status_set('blocked', msg)
        raise DNSHAException(msg)
    return True


def expect_ha():
    """ Determine if the unit expects to be in HA

    Check juju goal-state if ha relation is expected, check for VIP or dns-ha
    settings which indicate the unit should expect to be related to hacluster.

    @returns boolean
    """
    ha_related_units = []
    try:
        ha_related_units = list(expected_related_units(reltype='ha'))
    except (NotImplementedError, KeyError):
        pass
    return len(ha_related_units) > 0 or config('vip') or config('dns-ha')


def generate_ha_relation_data(service,
                              haproxy_enabled=True,
                              extra_settings=None):
    """ Generate relation data for ha relation

    Based on configuration options and unit interfaces, generate a json
    encoded dict of relation data items for the hacluster relation,
    providing configuration for DNS HA or VIP's + haproxy clone sets.

    Example of supplying additional settings::

        COLO_CONSOLEAUTH = 'inf: res_nova_consoleauth grp_nova_vips'
        AGENT_CONSOLEAUTH = 'ocf:openstack:nova-consoleauth'
        AGENT_CA_PARAMS = 'op monitor interval="5s"'

        ha_console_settings = {
            'colocations': {'vip_consoleauth': COLO_CONSOLEAUTH},
            'init_services': {'res_nova_consoleauth': 'nova-consoleauth'},
            'resources': {'res_nova_consoleauth': AGENT_CONSOLEAUTH},
            'resource_params': {'res_nova_consoleauth': AGENT_CA_PARAMS})
        generate_ha_relation_data('nova', extra_settings=ha_console_settings)


    @param service: Name of the service being configured
    @param extra_settings: Dict of additional resource data
    @returns dict: json encoded data for use with relation_set
    """
    _relation_data = {'resources': {}, 'resource_params': {}}

    if haproxy_enabled:
        _haproxy_res = 'res_{}_haproxy'.format(service)
        _relation_data['resources'] = {_haproxy_res: 'lsb:haproxy'}
        _relation_data['resource_params'] = {
            _haproxy_res: 'op monitor interval="5s"'
        }
        _relation_data['init_services'] = {_haproxy_res: 'haproxy'}
        _relation_data['clones'] = {
            'cl_{}_haproxy'.format(service): _haproxy_res
        }

    if extra_settings:
        for k, v in extra_settings.items():
            if _relation_data.get(k):
                _relation_data[k].update(v)
            else:
                _relation_data[k] = v

    if config('dns-ha'):
        update_hacluster_dns_ha(service, _relation_data)
    else:
        update_hacluster_vip(service, _relation_data)

    return {
        'json_{}'.format(k): json.dumps(v, **JSON_ENCODE_OPTIONS)
        for k, v in _relation_data.items() if v
    }


def update_hacluster_dns_ha(service, relation_data,
                            crm_ocf='ocf:maas:dns'):
    """ Configure DNS-HA resources based on provided configuration

    @param service: Name of the service being configured
    @param relation_data: Pointer to dictionary of relation data.
    @param crm_ocf: Corosync Open Cluster Framework resource agent to use for
                    DNS HA
    """
    # Validate the charm environment for DNS HA
    assert_charm_supports_dns_ha()

    settings = ['os-admin-hostname', 'os-internal-hostname',
                'os-public-hostname', 'os-access-hostname']

    # Check which DNS settings are set and update dictionaries
    hostname_group = []
    for setting in settings:
        hostname = config(setting)
        if hostname is None:
            log('DNS HA: Hostname setting {} is None. Ignoring.'
                ''.format(setting),
                DEBUG)
            continue
        m = re.search('os-(.+?)-hostname', setting)
        if m:
            endpoint_type = m.group(1)
            # resolve_address's ADDRESS_MAP uses 'int' not 'internal'
            if endpoint_type == 'internal':
                endpoint_type = 'int'
        else:
            msg = ('Unexpected DNS hostname setting: {}. '
                   'Cannot determine endpoint_type name'
                   ''.format(setting))
            status_set('blocked', msg)
            raise DNSHAException(msg)

        hostname_key = 'res_{}_{}_hostname'.format(service, endpoint_type)
        if hostname_key in hostname_group:
            log('DNS HA: Resource {}: {} already exists in '
                'hostname group - skipping'.format(hostname_key, hostname),
                DEBUG)
            continue

        hostname_group.append(hostname_key)
        relation_data['resources'][hostname_key] = crm_ocf
        relation_data['resource_params'][hostname_key] = (
            'params fqdn="{}" ip_address="{}"'
            .format(hostname, resolve_address(endpoint_type=endpoint_type,
                                              override=False)))

    if len(hostname_group) >= 1:
        log('DNS HA: Hostname group is set with {} as members. '
            'Informing the ha relation'.format(' '.join(hostname_group)),
            DEBUG)
        relation_data['groups'] = {
            DNSHA_GROUP_NAME.format(service=service): ' '.join(hostname_group)
        }
    else:
        msg = 'DNS HA: Hostname group has no members.'
        status_set('blocked', msg)
        raise DNSHAException(msg)


def get_vip_settings(vip):
    """Calculate which nic is on the correct network for the given vip.

    If nic or netmask discovery fail then fallback to using charm supplied
    config. If fallback is used this is indicated via the fallback variable.

    @param vip: VIP to lookup nic and cidr for.
    @returns (str, str, bool): eg (iface, netmask, fallback)
    """
    iface = get_iface_for_address(vip)
    netmask = get_netmask_for_address(vip)
    fallback = False
    if iface is None:
        iface = config('vip_iface')
        fallback = True
    if netmask is None:
        netmask = config('vip_cidr')
        fallback = True
    return iface, netmask, fallback


def update_hacluster_vip(service, relation_data):
    """ Configure VIP resources based on provided configuration

    @param service: Name of the service being configured
    @param relation_data: Pointer to dictionary of relation data.
    """
    cluster_config = get_hacluster_config()
    vip_group = []
    vips_to_delete = []
    for vip in cluster_config['vip'].split():
        if is_ipv6(vip):
            res_vip = 'ocf:heartbeat:IPv6addr'
            vip_params = 'ipv6addr'
        else:
            res_vip = 'ocf:heartbeat:IPaddr2'
            vip_params = 'ip'

        iface, netmask, fallback = get_vip_settings(vip)

        vip_monitoring = 'op monitor depth="0" timeout="20s" interval="10s"'
        if iface is not None:
            # NOTE(jamespage): Delete old VIP resources
            # Old style naming encoding iface in name
            # does not work well in environments where
            # interface/subnet wiring is not consistent
            vip_key = 'res_{}_{}_vip'.format(service, iface)
            if vip_key in vips_to_delete:
                vip_key = '{}_{}'.format(vip_key, vip_params)
            vips_to_delete.append(vip_key)

            vip_key = 'res_{}_{}_vip'.format(
                service,
                hashlib.sha1(vip.encode('UTF-8')).hexdigest()[:7])

            relation_data['resources'][vip_key] = res_vip
            # NOTE(jamespage):
            # Use option provided vip params if these where used
            # instead of auto-detected values
            if fallback:
                relation_data['resource_params'][vip_key] = (
                    'params {ip}="{vip}" cidr_netmask="{netmask}" '
                    'nic="{iface}" {vip_monitoring}'.format(
                        ip=vip_params,
                        vip=vip,
                        iface=iface,
                        netmask=netmask,
                        vip_monitoring=vip_monitoring))
            else:
                # NOTE(jamespage):
                # let heartbeat figure out which interface and
                # netmask to configure, which works nicely
                # when network interface naming is not
                # consistent across units.
                relation_data['resource_params'][vip_key] = (
                    'params {ip}="{vip}" {vip_monitoring}'.format(
                        ip=vip_params,
                        vip=vip,
                        vip_monitoring=vip_monitoring))

            vip_group.append(vip_key)

    if vips_to_delete:
        try:
            relation_data['delete_resources'].extend(vips_to_delete)
        except KeyError:
            relation_data['delete_resources'] = vips_to_delete

    if len(vip_group) >= 1:
        key = VIP_GROUP_NAME.format(service=service)
        try:
            relation_data['groups'][key] = ' '.join(vip_group)
        except KeyError:
            relation_data['groups'] = {
                key: ' '.join(vip_group)
            }
