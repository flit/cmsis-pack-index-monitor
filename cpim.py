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
from typing import List, Optional, Sequence, Tuple, Union
import requests
from xml.etree import ElementTree
from urllib.parse import urljoin
import dateutil.parser
from time import sleep
import colorama
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum

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

class FailureCause(Enum):
    CONNECT_FAILED = 1
    REQUEST_FAILED = 2
    REQUEST_TIMEOUT = 3
    INVALID_DATA = 4

@dataclass
class RequestFailureInfo:
    url: str
    status: int = -1
    cause: FailureCause = FailureCause.REQUEST_FAILED
    response: Optional[requests.Response] = None
    pdsc: Optional[PdscInfo] = None

class RequestError(Exception):
    pass

class PackIndexMonitor:
    PIDX = "http://www.keil.com/pack/index.pidx"

    REQUEST_TIMEOUT_SECONDS = 30

    def __init__(self, vendors: Sequence[str], quiet: bool) -> None:
        self._vendors = vendors
        self._all_vendors = '*' in vendors
        self._quiet = quiet

    def retrieve_index(self) -> Tuple[datetime, List[PdscInfo]]:
        try:
            idx_response = requests.get(self.PIDX, timeout=self.REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.ConnectionError:
            raise RequestError(RequestFailureInfo(url=self.PIDX, cause=FailureCause.CONNECT_FAILED))
        except requests.exceptions.Timeout:
            raise RequestError(RequestFailureInfo(url=self.PIDX, cause=FailureCause.REQUEST_TIMEOUT))

        if not self._quiet:
            print(f"Pack index response status: {idx_response.status_code}")
        if idx_response.status_code != 200:
            if self._quiet:
                print(f"Failed to retrieve pack index! Response status: {idx_response.status_code}")
            raise RequestError(RequestFailureInfo(
                    url=self.PIDX,
                    status=idx_response.status_code,
                    response=idx_response,
                    ))

        # Parse XML
        try:
            idx = ElementTree.XML(idx_response.content)
        except ElementTree.ParseError:
            raise RequestError(RequestFailureInfo(url=self.PIDX, cause=FailureCause.INVALID_DATA))

        # Get timestamp
        ts_iso = idx.findtext('timestamp')
        if ts_iso is None:
            print("Error: missing index timestamp!")
            raise RequestError(RequestFailureInfo(url=self.PIDX, cause=FailureCause.INVALID_DATA))
        
        try:
            ts = dateutil.parser.isoparse(ts_iso)
        except dateutil.parser.ParserError:
            raise RequestError(RequestFailureInfo(url=self.PIDX, cause=FailureCause.INVALID_DATA))

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

    def retrieve_pdsc(self, pdsc: PdscInfo) -> Union[requests.Response, RequestFailureInfo]:
        try:
            return requests.get(pdsc.get_pdsc_url(), timeout=self.REQUEST_TIMEOUT_SECONDS)
        except requests.exceptions.ConnectionError:
            return RequestFailureInfo(url=pdsc.get_pdsc_url(), cause=FailureCause.CONNECT_FAILED)
        except requests.exceptions.Timeout:
            return RequestFailureInfo(url=pdsc.get_pdsc_url(), cause=FailureCause.REQUEST_TIMEOUT)

    def check_pdscs(self) -> List[RequestFailureInfo]:
        try:
            ts, pdscs = self.retrieve_index()
        except RequestError as err:
            return [err.args[0]]

        if not self._quiet:
            print(f"Timestamp: {ts}")
            print(f"{len(pdscs)} total packs")

        if self._all_vendors:
            filtered_pdscs = pdscs
        else:
            filtered_pdscs = [
                p
                for p in pdscs
                if p.vendor.casefold() in self._vendors
            ]
        if not self._quiet:
            print(f"{len(filtered_pdscs)} monitored packs")

        failures: List[RequestFailureInfo] = []

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
                if isinstance(response, RequestFailureInfo):
                    failures.append(response)
                    msg_file.write(f"{colorama.Fore.RED}{pdsc_url}{colorama.Style.RESET_ALL} [{response.cause.name}]")
                elif response.status_code != 200:
                    failures.append(RequestFailureInfo(
                        url=pdsc_url,
                        status=response.status_code,
                        cause=FailureCause.REQUEST_FAILED,
                        response=response,
                        pdsc=pdsc,
                        ))
                    msg_file.write(f"{colorama.Fore.RED}{pdsc_url} "
                                   f"{colorama.Style.BRIGHT}[{response.status_code}]{colorama.Style.RESET_ALL}")
                elif not self._quiet:
                    msg_file.write(f"{colorama.Fore.GREEN}{pdsc_url}{colorama.Style.RESET_ALL}")

        return failures


class PackIndexMonitorTool:
    DEFAULT_VENDORS = ["Keil"]

    def __init__(self) -> None:
        self._parser = self._build_parser()
    
    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        parser.add_argument('-i', '--interval', type=int, default=0,
            help="Set to non-zero interval in seconds for repeated checks.")
        parser.add_argument('-l', '--log', metavar='LOGFILE',
            help="Path to log file. Defaults to no log.")
        parser.add_argument('-v', '--vendors', nargs='+', action='extend', default=[],
            help="Set of vendors for which packs should be checked. If '*' is included in the list, "
                "then all packs will be checked. Default: " + ", ".join(self.DEFAULT_VENDORS))
        parser.add_argument('-q', '--quiet', action='store_true',
            help="Print only progress bar (if tty) and errors.")

        return parser
    
    def run(self) -> None:
        args = self._parser.parse_args()

        try:
            if args.log:
                logfile = open(args.log, 'a')
            else:
                logfile = None

            vendors = [
                vendor.strip().casefold()
                for vendor in (args.vendors or self.DEFAULT_VENDORS)
                ]

            mon = PackIndexMonitor(vendors, args.quiet)

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
                        if logfile:
                            logfile.write(f"    {fail.status} {fail.url}\n")
                            if fail.response is not None:
                                for hk, hv in fail.response.headers.items():
                                    logfile.write(f"        {hk}: {hv}\n")
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

