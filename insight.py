#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2018 PingCAP, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http:#www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This script is developed under Python 3.6 and compatiable with 3.2 and
# above, although Python 2 with version 2.7 and above may also work in
# most circumstances, please use latest Python 3 when possible.

import json
import logging
import os
import time

from file import configfiles
from file import logfiles
from metric import prometheus
from runtime import perf
from tidb import pdctl
from tidb import tidbinfo
from utils import cmd
from utils import fileopt
from utils import lsof
from utils import space
from utils import util

from runtime.ftrace import ftrace
from utils.process import meta as proc_meta


class Insight():
    cwd = util.cwd()
    # data output dir
    outdir = "data"
    alias = ""
    # data collected by `collector`
    collector_data = {}

    insight_perf = None
    insight_logfiles = None
    insight_configfiles = None
    insight_tidb = None
    insight_trace = None
    insight_metric = None
    insight_tui = None

    def __init__(self, args):
        if args.alias:
            self.alias = args.alias
        else:
            self.alias = util.get_hostname()

        if args.output and util.is_abs_path(args.output):
            self.outdir = args.output
            self.full_outdir = fileopt.create_dir(
                os.path.join(self.outdir, self.alias))
        else:
            if args.output:
                self.outdir = args.output
            self.full_outdir = fileopt.create_dir(
                os.path.join(self.cwd, self.outdir, self.alias))
        logging.debug("Output directory is: %s" % self.full_outdir)

    # parse process info in collector_data and build required dict
    def format_proc_info(self, keyname=None):
        result = {}
        if not keyname:
            return result
        for proc in self.collector_data["proc_stats"]:
            try:
                result[proc["pid"]] = proc[keyname]
            except KeyError:
                continue
        return result

    # collect data with `collector` and store it to disk
    def collector(self, args):
        # call `collector` and store data to output dir
        base_dir = os.path.join(util.pwd(), "../")
        collector_exec = os.path.join(base_dir, "bin/collector")
        collector_outdir = fileopt.create_dir(
            os.path.join(self.full_outdir, "collector"))

        if args.pid:
            logging.debug(
                "Collecting process infor only for PID %s" % args.pid)
            collector_exec = [collector_exec, '-proc', '-pid', '%s' % args.pid]
        elif args.port:
            protocol = 'UDP' if args.udp else 'TCP'
            pids = ','.join(
                str(_pid) for _pid in proc_meta.find_process_by_port(args.port, protocol))
            logging.debug("Collecting process infor for PIDs %s" % pids)
            collector_exec = [collector_exec, '-proc', '-pid', '%s' % pids]
        # else call collector without any argument

        stdout, stderr = util.run_cmd(collector_exec)
        if stderr:
            logging.info("collector output:" % str(stderr))
        try:
            self.collector_data = json.loads(stdout)
        except json.JSONDecodeError:
            logging.critical("Error collecting system info:\n%s" % stderr)
            return

        # save various info to seperate .json files
        for k, v in self.collector_data.items():
            # This is a dirty hack to omit empty results, until Go fix that upstream,
            # see: https://github.com/golang/go/issues/11939
            if (args.pid or args.port) and k in ['sysinfo', 'ntp']:
                continue
            if not v or len(v) < 1:
                logging.debug("Skipped empty result %s:%s" % (k, v))
                continue
            fileopt.write_file(os.path.join(collector_outdir, "%s.json" % k),
                               json.dumps(v, indent=2))

    def run_vmtouch(self, args):
        if args.subcmd_runtime != "vmtouch":
            logging.debug("Ingoring collecting of vmtouch data.")
            return
        if not args.target:
            return

        base_dir = os.path.join(util.pwd(), "../")
        vmtouch_exec = os.path.join(base_dir, "bin/vmtouch")
        vmtouch_outdir = fileopt.create_dir(
            os.path.join(self.full_outdir, "vmtouch"))
        if not vmtouch_outdir:
            return

        stdout, stderr = util.run_cmd(
            [vmtouch_exec, "-v", args.target])
        if stderr:
            logging.info("vmtouch output: %s" % str(stderr))
            return
        fileopt.write_file(os.path.join(vmtouch_outdir, "%s_%d.txt" % (
            args.target.replace("/", "_"), (time.time() * 1000))), str(stdout))

    def run_blktrace(self, args):
        if args.subcmd_runtime != "blktrace":
            logging.debug("Ingoring collecting of blktrace data.")
            return
        if not args.target:
            return

        blktrace_outdir = fileopt.create_dir(
            os.path.join(self.full_outdir, "blktrace"))
        if not blktrace_outdir:
            return

        time = 60
        if args.time:
            time = args.time
        util.run_cmd_for_a_while(
            ["blktrace", "-d", args.target, "-D", blktrace_outdir], time)

    def run_perf(self, args):
        if args.subcmd_runtime != "perf":
            logging.debug("Ignoring collecting of perf data.")
            return
        # perf requires root priviledge
        if not util.is_root_privilege():
            logging.fatal("It's required to run perf with root priviledge.")
            return

        # "--auto" has the highest priority
        if args.auto:
            # build dict of pid to process name
            perf_proc = self.format_proc_info("name")
        # parse pid list
        elif args.pid:
            perf_proc = {}
            for _pid in args.pid:
                perf_proc[_pid] = None
        # find process by port
        elif args.listen_port:
            perf_proc = {}
            pid_list = proc_meta.find_process_by_port(
                args.listen_port, args.listen_proto)
            if not pid_list or len(pid_list) < 1:
                return
            for _pid in pid_list:
                perf_proc[_pid] = None
        self.insight_perf = perf.Perf(
            args, self.full_outdir, 'perfdata', perf_proc)
        self.insight_perf.run_collecting()

    def run_ftrace(self, args):
        if args.subcmd_runtime != "ftrace":
            logging.debug("Ignoring collecting of ftrace data.")
            return
        # perf requires root priviledge
        if not util.is_root_privilege():
            logging.fatal("It's required to run ftrace with root priviledge.")
            return

        if args.ftracepoint:
            self.insight_ftrace = ftrace.Ftrace(
                args, self.full_outdir, 'ftracedata', self.cwd)
            self.insight_ftrace.run_collecting()
        else:
            logging.debug(
                "Ignoring collecting of ftrace data, no tracepoint is chose.")

    def get_datadir_size(self):
        # du requires root priviledge to check data-dir
        if not util.is_root_privilege():
            logging.fatal(
                "It's required to check data-dir size with root priviledge.")
            return

        for proc in self.collector_data["proc_stats"]:
            args = util.parse_cmdline(proc["cmd"])
            try:
                data_dir = args["data-dir"]
            except KeyError:
                logging.debug(
                    "'data-dir' is not set in cmdline args: %s" % args)
                continue
            if os.listdir(data_dir) != []:
                stdout, stderr = space.du_subfiles(data_dir)
            else:
                stdout, stderr = space.du_total(data_dir)
            if stdout:
                fileopt.write_file(os.path.join(self.full_outdir, "size-%s" % proc["pid"]),
                                   stdout)
            if stderr:
                fileopt.write_file(os.path.join(self.full_outdir, "size-%s.err" % proc["pid"]),
                                   stderr)

    def get_lsof_tidb(self):
        # lsof requires root priviledge
        if not util.is_root_privilege():
            logging.fatal("It's required to run lsof with root priviledge.")
            return

        for proc in self.collector_data["proc_stats"]:
            stdout, stderr = lsof.lsof(proc["pid"])
            if stdout:
                fileopt.write_file(os.path.join(self.full_outdir, "lsof-%s") % proc["pid"],
                                   stdout)
            if stderr:
                fileopt.write_file(os.path.join(self.full_outdir, "lsof-%s.err" % proc["pid"]),
                                   stderr)

    def save_logfiles(self, args):
        # reading logs requires root priviledge
        if not util.is_root_privilege():
            logging.warning("It's required to read logs with root priviledge.")
            # return

        self.insight_logfiles = logfiles.InsightLogFiles(
            args, self.full_outdir, 'logs')
        proc_cmdline = None
        if args.auto:
            proc_cmdline = self.format_proc_info("cmd")  # cmdline of process
        self.insight_logfiles.run_collecting(proc_cmdline)

    def save_configs(self, args):
        self.insight_configfiles = configfiles.InsightConfigFiles(
            args, self.full_outdir, 'configs')
        # collect TiDB configs
        proc_cmdline = None
        if args.auto:
            proc_cmdline = self.format_proc_info("cmd")  # cmdline of process
        self.insight_configfiles.run_collecting(proc_cmdline)

    def read_apis(self, args):
        if args.subcmd_tidb == "pdctl":
            # read and save `pd-ctl` info
            self.insight_tidb = pdctl.PDCtl(args, self.full_outdir, 'pdctl')
            self.insight_tidb.run_collecting()
        elif args.subcmd_tidb == 'tidbinfo':
            # read and save TiDB's server info
            self.insight_tidb = tidbinfo.TiDBInfo(
                args, self.full_outdir, 'tidbinfo')
            self.insight_tidb.run_collecting()

    def dump_metrics(self, args):
        if args.subcmd_metric == "prom":
            self.insight_metric = prometheus.PromMetrics(
                args, self.full_outdir, 'metric/prometheus')
            self.insight_metric.run_collecting()
        pass


if __name__ == "__main__":
    # WIP: add params to set output dir / overwriting on non-empty target dir
    args = cmd.parse_insight_opts()
    if args.verbose:
        logging.basicConfig(
            format='[%(levelname)s] %(message)s (at %(filename)s:%(lineno)d in %(funcName)s).',
            level=logging.DEBUG)
        logging.info("Debug logging enabled.")
        logging.debug("Input arguments are: %s" % args)
    else:
        logging.basicConfig(
            format='[%(levelname)s] %(message)s.', level=logging.INFO)
        logging.info("Using logging level: INFO.")

    # display information, read-only functions are excuted before any others
    if args.subcmd == "show":
        if args.subcmd_show in ["servers"]:
            from explorer import server
            insight_tui = server.TUIServerList(args)
        elif args.subcmd_show in ["server"]:
            from explorer import server
            insight_tui = server.TUIServerInfo(args)
        elif args.subcmd_show in ["tidb", "tikv", "pd"]:
            from explorer import modules
            insight_tui = modules.TUIModule(args)
        elif args.subcmd_show in ["summary"]:
            from explorer import summary
            insight_tui = summary.TUISummary(args)
        insight_tui.display()
        exit(0)

    # re-import dumped data
    if args.subcmd == 'metric' and args.subcmd_metric == "load":
        from metric.importer import prometheus as import_prom
        insight_importer = import_prom.PromDump(args)
        insight_importer.run_importing()
        exit(0)

    if not util.is_root_privilege():
        logging.warning("""Running TiDB Insight with non-superuser privilege may result
          in lack of some information or data in the final output, if
          you find certain data missing or empty in result, please try
          to run this script again with root.""")

    insight = Insight(args)

    # compress all output to tarball
    if args.subcmd == "archive":
        if args.extract:
            fileopt.decompress_tarball_recursive(args.input, insight.outdir)
            # try once more for multi-level tarballs
            fileopt.decompress_tarball_recursive(
                insight.outdir, insight.outdir)
        else:
            fileopt.compress_tarball(insight.outdir, insight.alias)

    try:
        if args.auto:
            logging.debug(
                "In auto mode, basic information is collected by default.")
            insight.collector(args)
            # check size of data folder of TiDB processes
            insight.get_datadir_size()
            # list files opened by TiDB processes
            insight.get_lsof_tidb()
    except AttributeError:
        logging.debug("Auto mode not detected and disabled.")
        pass

    if args.subcmd == "system" and args.collector:
        insight.collector(args)

    # WIP: call scripts that collect metrics of the node
    if args.subcmd == "runtime":
        insight.run_perf(args)
        # save ftrace data
        insight.run_ftrace(args)
        # save vmtouch data
        insight.run_vmtouch(args)
        # save blktrace data
        insight.run_blktrace(args)

    # save log files
    if args.subcmd == "log":
        insight.save_logfiles(args)
    # save config files
    if args.subcmd == "config":
        insight.save_configs(args)

    if args.subcmd == "tidb":
        # read and save info from TiDB related APIs
        insight.read_apis(args)

    if args.subcmd == "metric":
        insight.dump_metrics(args)
