#!/usr/bin/env python3
"""Implements BootstackQaTests."""

import json
import logging
import requests
import time
import unittest

from zaza import model


TEST_TIMEOUT = 600


def nagios_error_allowed(service, allowed_nagios_errors):
    """Check if any string in allowed_nagios_errors is in service."""
    for err in allowed_nagios_errors:
        if err in service:
            return True

    return False


class TestBase(unittest.TestCase):
    """Base class for functional charm tests."""

    @classmethod
    def setUpClass(cls):
        """Run setup for tests."""
        cls.model_name = model.get_juju_model()
        cls.deployed_apps = model.sync_deployed()
        # list of deploy applications to try upgrade
        # note not the charm names, the application names.
        cls.charms = [
            "canonical-livepatch",
            "elastic",
            "fstab-config",
            "grafana",
            "graylog",
            "hw-health",
            "mongo",
            "nagios",
            "nrpe",
            "openstack-service-checks",
            "prometheus",
            "prometheus-alertmanager",
            "prometheus-ceph-exporter",
            "prometheus-openstack-exporter",
            "prometheus-libvirt-exporter",
            "sysconfig",
            "telegraf",
            "thruk-agent",
        ]
        cls.nagios_unit = model.get_lead_unit_name('nagios')
        cls.grafana_unit = model.get_lead_unit_name('grafana')
        cls.grafana_ip = model.get_app_ips('grafana')[0]

    @property
    def grafana_admin_password(self):
        """Get grafana admin password."""
        action = model.run_action(
            self.grafana_unit,
            "get-admin-password",
            raise_on_failure=True,
        )
        self._admin_pass = action.data["results"]["password"]
        return self._admin_pass

    def get_and_check_dashboards(self, cond):
        """Retrieve dashboard from grafana.

        Check if cond(dashboards) is true, otherwise retry up to TEST_TIMEOUT
        """
        timeout = time.time() + TEST_TIMEOUT
        dashboards = []
        while True:
            req = requests.get(
                "http://{host}:{port}"
                "/api/search?dashboardIds".format(
                    host=self.grafana_ip, port="3000"
                ),
                auth=("admin", self.grafana_admin_password),
            )
            self.assertTrue(req.status_code == 200)
            dashboards = json.loads(req.text)
            if time.time() > timeout or cond(dashboards):
                break
            time.sleep(30)
        return dashboards

    def is_grafana_api_ready(self):
        """Verify if the API is ready.

        Curl the api endpoint.
        We'll retry until the TEST_TIMEOUT.
        """
        test_command = "{} -I 127.0.0.1 -p {} -u {}".format(
            "/usr/lib/nagios/plugins/check_http",
            "3000",
            "/",
        )
        timeout = time.time() + TEST_TIMEOUT
        while time.time() < timeout:
            response = model.run_on_unit(self.grafana_unit, test_command)
            if response["Code"] == "0":
                return
            logging.warning(
                "Unexpected check_http response: {}. Retrying in 30s.".format(response)
            )
            time.sleep(30)

        # we didn't get rc=0 in the allowed time, fail the test
        self.fail(
            "http port didn't respond to the command \n"
            "'{test_command}' as expected.\n"
            "Result: {result}".format(test_command=test_command, result=response)
        )

    def grafana_imported_dashboards(self):
        """Check that Grafana dashboards expected are there."""
        dashboards = self.get_and_check_dashboards(lambda dashes: len(dashes) >= 4)
        self.assertTrue(len(dashboards) >= 4)
        self.assertTrue(
            all(
                dash.get("type", "") == "dash-db"
                and dash.get("uri", "").startswith("db/juju-")
                for dash in dashboards
            )
        )

    def charm_functests(self):
        """Run some basic functests on the deployed bundle.

        Tests to write:
        - grafana hosts a known list of dashboards
          GET /api/search?folderIds=0&query=&starred=false HTTP/1.1
        - metrics are available from the prometheus exporters and telegraf
        - graylog has entries in the stream

        """
        self.nagios_functests()
        self.is_grafana_api_ready()
        self.grafana_imported_dashboards()

    def nagios_functests(self):
        """Nagios tests."""
        # run on unit:
        query_command = (
            'printf "GET services\nColumns: host_name description '
            'state\n" | unixcat /var/lib/nagios3/livestatus/socket'
        )
        nag_status = model.run_on_unit(self.nagios_unit, query_command)
        # list of services that are allowed to be anything other than OK:
        allowed_nagios_errors = [
            "juju-mysql-0-mysql",
            "juju-openstack-service-checks-0-cinder_services",
            "check_load",
        ]
        services = set()

        # first, force all checks to run right now, or we have to wait for 5 mins
        force_check = []
        ts = time.time()

        for line in nag_status["stdout"].splitlines():
            # each line is [hostname, service_name, status]
            fields = line.split(";")
            force_check.append(
                f"SCHEDULE_FORCED_SVC_CHECK;{fields[0]};{fields[1]};{ts}"
            )
        model.run_on_unit(self.nagios_unit, "\n".join(force_check))
        logging.info("Waiting for 60s for Nagios to execute checks")
        time.sleep(60)  # give Nagios time to execute the checks
        # Get an updated status from Nagios
        nag_status = model.run_on_unit(self.nagios_unit, query_command)
        # parse nag_status, fail if unexpected errors

        for line in nag_status["stdout"].splitlines():
            fields = line.split(";")
            services.add(fields[1])

            if int(fields[2]) > 0:
                if nagios_error_allowed(fields[2], allowed_nagios_errors):
                    self.fail("Nagios service {} is in error state".format(fields[1]))
        # check list of services incldues the ones we want to service
        expected_services = {
            "juju-ceph-mon-0-ceph",
            "juju-ceph-osd-0-ceph-osd",
            "juju-grafana-0-grafana_http",
            "juju-openstack-service-checks-0-keystone_public",
            "juju-prometheus-ceph-exporter-0-prometheus_ceph_exporter_http",
            "juju-prometheus-openstack-exporter-0-prometheus_openstack_exporter_http",
        }

        if not expected_services.issubset(services):
            # some services are missing
            self.fail("Nagios is missing services: {}".format(
                expected_services - services))


class BootstackCandidateUpgrade(TestBase):
    """QA tests for charm release.

    Runs a deploy of the bundle using current stable charms, then tests upgrade.
    """

    def test10_upgrade_charms(self):
        """Test upgrade charm to candidate channel."""
        for application_name in self.charms:
            if application_name in self.deployed_apps:
                logging.info(f"Running charm upgrade for {application_name}")
                try:
                    model.upgrade_charm(
                        application_name,
                        channel="candidate",
                        model_name=self.model_name,
                    )
                except Exception as e:
                    self.fail(
                        "Failed to upgrade charm %s with %s" % (application_name, e)
                    )
        model.block_until_all_units_idle(self.model_name)

    def test11_test_charms(self):
        """Test charm functionality after upgrade."""
        self.charm_functests()


class BootstackCandidateInstall(TestBase):
    """QA tests for charm release.

    Runs a deploy of the bundle using candidate charms.
    """

    def test11_test_charms(self):
        """Test charm functionality for candidate charms."""
        self.charm_functests()
