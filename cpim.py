#!/usr/bin/env python3
# Copyright (c) 2021 Chris Reed
#
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple
import requests
from xml.etree.ElementTree import XML
from urllib.parse import urljoin
import dateutil.parser
from time import sleep
import colorama
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed

colorama.init()

@dataclass
class PdscInfo:
    url: str
    vendor: str
    name: str
    version: str

    def get_pdsc_url(self) -> str:
        prefix_url = self.url if self.url.endswith("/") else self.url + "/"
        return urljoin(prefix_url, f"{self.vendor}.{self.name}.pdsc")

@dataclass
class RequestFailureInfo:
    url: str
    status: int

class RequestError(Exception):
    pass

class PackIndexMonitor:
    PIDX = "http://www.keil.com/pack/index.pidx"

    VENDORS_TO_MONITOR = ("Keil",)

    def __init__(self) -> None:
        pass

    def retrieve_index(self) -> Tuple[datetime, List[PdscInfo]]:
        idx_response = requests.get(self.PIDX)
        print(f"Pack index response status: {idx_response.status_code}")

        if idx_response.status_code != 200:
            raise RequestError(RequestFailureInfo(url=self.PIDX, status=idx_response.status_code))

        # Parse XML
        idx = XML(idx_response.content)

        # Get timestamp
        ts_iso = idx.findtext('timestamp')
        if ts_iso is None:
            raise RuntimeError("Error: missing index timestamp!")
        ts = dateutil.parser.isoparse(ts_iso)

        # Get list of pdscs.
        pdscs = [
            PdscInfo(
                url=e.attrib['url'],
                vendor=e.attrib['vendor'],
                name=e.attrib['name'],
                version=e.attrib['version']
                )
            for e in idx.iterfind('pindex/pdsc')
        ]

        return ts, pdscs

    def check_pdscs(self) -> List[RequestFailureInfo]:
        try:
            ts, pdscs = self.retrieve_index()
        except RequestError as err:
            return [err.args[0]]

        print(f"Timestamp: {ts}")
        print(f"{len(pdscs)} total packs")

        filtered_pdscs = [
            p
            for p in pdscs
            if p.vendor in self.VENDORS_TO_MONITOR
        ]
        print(f"{len(filtered_pdscs)} monitored packs")

        failures = []

        with ThreadPoolExecutor(max_workers=32) as executor:
            futures_map = {
                executor.submit(requests.get, pdsc.get_pdsc_url()): pdsc
                for pdsc in filtered_pdscs
            }

            if sys.stdout.isatty():
                futures_iter = tqdm(as_completed(futures_map), total=len(filtered_pdscs), unit="pack")
                msg_file = futures_iter
            else:
                futures_iter = futures_map
                msg_file = sys.stdout

            for future in futures_iter:
                pdsc = futures_map[future]
                pdsc_url = pdsc.get_pdsc_url()
                response = future.result()
                if response.status_code != 200:
                    failures.append(RequestFailureInfo(url=pdsc_url, status=response.status_code))
                    msg_file.write(f"{colorama.Fore.RED}{pdsc_url}{colorama.Style.RESET_ALL}")
                else:
                    msg_file.write(f"{colorama.Fore.GREEN}{pdsc_url}{colorama.Style.RESET_ALL}")

        return failures



class PackIndexMonitorTool:
    def __init__(self) -> None:
        self._parser = self._build_parser()
    
    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.add_argument('--interval', type=int, default=0,
            help="Set to non-zero interval in seconds for repeated checks.")
        parser.add_argument('--log', metavar='LOGFILE',
            help="Path to log file. Defaults to no log.")

        return parser
    
    def run(self) -> None:
        args = self._parser.parse_args()

        try:
            if args.log:
                logfile = open(args.log, 'a')
            else:
                logfile = None

            mon = PackIndexMonitor()

            while True:
                now = datetime.now()
                if logfile:
                    logfile.write(f"{now}: Started checking index\n")

                failures = mon.check_pdscs()
                now = datetime.now()
                if failures:
                    print(f"{now}: {colorama.Fore.RED}{len(failures)} failures{colorama.Style.RESET_ALL}")
                    if logfile:
                        logfile.write(f"{now}: {len(failures)} failures\n")

                    for fail in failures:
                        # print(f"    {fail.status} {fail.url}")
                        if logfile:
                            logfile.write(f"    {fail.status} {fail.url}\n")
                else:
                    print(f"{now}: {colorama.Fore.GREEN}No failures!{colorama.Style.RESET_ALL}")
                    if logfile:
                        logfile.write(f"{now}: No failures!\n")

                if logfile:
                    now = datetime.now()
                    logfile.write(f"{now}: Finished checking index\n")

                if args.interval == 0:
                    break
                sleep(args.interval)

        except KeyboardInterrupt:
            print("Interrupted by user")


if __name__ == '__main__':
    PackIndexMonitorTool().run()

