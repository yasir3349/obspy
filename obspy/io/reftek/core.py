# -*- coding: utf-8 -*-
"""
REFTEK130 read support.
"""
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
from future.builtins import *  # NOQA

import os
import traceback
import warnings
import codecs

import numpy as np

from obspy import Trace, Stream
from obspy.io.mseed.util import _unpack_steim_1

from .header import PACKETS_IMPLEMENTED, PAYLOAD
from .util import _parse_short_time


def _is_reftek130(filename):
    """
    Checks whether a file is REFTEK130 format or not.

    :type filename: str
    :param filename: REFTEK130 file to be checked.
    :rtype: bool
    :return: ``True`` if a REFTEK130 file.

    Checks if overall length of file is consistent (i.e. multiple of 1024
    bytes) and checks for valid packet type identifiers in all expected packet
    positions.
    """
    if not os.path.isfile(filename):
        return False
    filesize = os.stat(filename).st_size
    # check if overall file size is a multiple of 1024
    if filesize < 1024 or filesize % 1024 != 0:
        return False

    with open(filename, 'rb') as fp:
        # check each expected packet's type header field
        while True:
            packet_type = fp.read(2).decode("ASCII", "ignore")
            if not packet_type:
                break
            if packet_type not in PACKETS_IMPLEMENTED:
                return False
            fp.seek(1022, os.SEEK_CUR)
    return True


def _read_reftek130(filename, network="", location="", component_codes=None,
                    **kwargs):
    """
    Read a REFTEK130 file into an ObsPy Stream.

    :type filename: str
    :param filename: REFTEK130 file to be checked.
    :type network: str
    :param network: Network code to fill in for all data (network code is not
        stored in EH/ET/DT packets).
    :type location: str
    :param location: Location code to fill in for all data (network code is not
        stored in EH/ET/DT packets).
    :type component_codes: list
    :param component_codes: Iterable of single-character component codes (e.g.
        ``['Z', 'N', 'E']``) to be appended to two-character stream name parsed
        from event header packet (e.g. ``'HH'``) for each of the channels in
        the data (e.g. to make the channel codes in a three channel data file
        to ``'HHZ'``, ``'HHN'``, ``'HHE'`` in the created stream object).
    """
    # read all packets from file, sort by packet sequence number
    packets = _read_into_packetlist(filename)
    packets = sorted(packets, key=lambda x: x.packet_sequence)
    try:
        if not packets:
            msg = ("Could not extract any data packets from file.")
            raise Exception(msg)
        # check if packet sequence is uninterrupted
        np.testing.assert_array_equal(
            np.bincount(np.diff([p.packet_sequence
                                 for p in packets]).astype(np.int_)),
            [0, len(packets) - 1])
    except AssertionError:
        # for now only support uninterrupted packet sequences
        msg = ("Reftek files with non-contiguous packet sequences are not "
               "yet implemented. Please open an issue on GitHub and provide "
               "a small (< 50kb) test file.")
        raise NotImplementedError(msg)
    # drop everything up to first EH packet
    p = packets.pop(0)
    dropped_packets = 0
    while p.type != "EH":
        p = packets.pop(0)
        dropped_packets += 1
    if dropped_packets:
        # warn if packet sequence does not start with EH packet
        msg = ("Reftek file does not start with EH (event header) packet. "
               "Dropped {:d} packet(s) at the start until encountering the "
               "first EH packet.").format(dropped_packets)
        warnings.warn(msg)
    eh = p
    # set common header fields from EH packet
    header = {
        "network": network,
        "station": (p.station_name + p.station_name_extension).strip(),
        "location": location, "sampling_rate": p.sampling_rate,
        "reftek130": eh._to_dict()}
    # set up a list of data (DT) packets per channel number
    data = {}
    p = packets.pop(0)
    while p.type == "DT":
        # only "C0" encoding supported right now
        if p.data_format != "C0":
            msg = ("Reftek data encoding '{}' not implemented yet. Please "
                   "open an issue on GitHub and provide a small (< 50kb) "
                   "test file.").format(p.type)
            raise NotImplementedError(msg)
        data.setdefault(p.channel_number, []).append(
            (p.time, p.packet_sequence, p.number_of_samples, p.sample_data))
        if not packets:
            break
        p = packets.pop(0)
    # expecting an ET packet at the end
    if p.type != "ET":
        msg = ("Data not ending with an ET (event trailer) package. Data "
               "might be unexpectedly truncated.")
        warnings.warn(msg)

    st = Stream()
    delta = 1.0 / eh.sampling_rate
    for channel_number, data_ in data.items():
        # sort by start time of packet (should not be necessary, in principle,
        # as we sorted by packet sequence number already.. but safety first)
        data_ = sorted(data_)
        # split data into a list of contiguous blocks
        data_contiguous = []
        chunk = data_.pop(0)
        chunk_list = [chunk]
        while data_:
            chunk = data_.pop(0)
            t, _, npts, _ = chunk
            if chunk_list:
                t_last, _, npts_last, _ = chunk_list[-1]
                # check if next starttime matches seamless to last chunk
                if t != t_last + npts_last * delta:
                    # gap/overlap, so start new contiguous list
                    data_contiguous.append(chunk_list)
                    chunk_list = [chunk]
                    continue
            # otherwise add to current chunk list
            chunk_list.append(chunk)
        data_contiguous.append(chunk_list)
        # read each contiguous block into one trace
        for data_ in data_contiguous:
            starttime = data_[0][0]
            data = []
            npts = 0
            for _, _, npts_, dat_ in data_:
                piece = _unpack_steim_1(data_string=dat_[44:],
                                        npts=npts_, swapflag=1)
                data.append(piece)
                npts += npts_
            data = np.hstack(data)

            tr = Trace(data=data, header=header.copy())
            tr.stats.starttime = starttime
            # if component codes were explicitly provided, use them together
            # with the stream label
            if component_codes is not None:
                tr.stats.channel = (
                    eh.stream_name.strip() + component_codes[channel_number])
            # otherwise check if channel code is set for the given channel
            # (seems to be not the case usually)
            elif eh.channel_code[channel_number] is not None:
                tr.stats.channel = eh.channel_code[channel_number]
            # otherwise fall back to using the stream label together with the
            # number of the channel in the file (starting with 0, as Z-1-2 is
            # common use for data streams not oriented against North)
            else:
                msg = ("No channel code specified in the data file and no "
                       "component codes specified. Using stream label and "
                       "number of channel in file as channel codes.")
                warnings.warn(msg)
                tr.stats.channel = eh.stream_name.strip() + str(channel_number)
            # check if endtime of trace is consistent
            t_last, _, npts_last, _ = data_[-1]
            try:
                assert npts == len(data)
                assert tr.stats.endtime == t_last + (npts_last - 1) * delta
                assert tr.stats.endtime == (
                    tr.stats.starttime + (npts - 1) * delta)
            except AssertionError:
                msg = ("Reftek file has a trace with an inconsistent endtime "
                       "or number of samples. Please open an issue on GitHub "
                       "and provide your file for testing.")
                raise Exception(msg)
            st += tr

    return st


class Packet(object):
    """
    """
    def __init__(self, type, experiment_number, year, unit_id, time,
                 byte_count, packet_sequence, payload):
        if type not in PACKETS_IMPLEMENTED:
            msg = "Invalid packet type: '{}'".format(type)
            raise ValueError(msg)
        self.type = type
        self.experiment_number = experiment_number
        self.unit_id = unit_id
        self.byte_count = byte_count
        self.packet_sequence = packet_sequence
        self.time = year and _parse_short_time(year, time) or None
        self._parse_payload(payload)

    def __str__(self):
        keys = ("experiment_number", "unit_id", "time", "byte_count",
                "packet_sequence")
        info = ["{}: {}".format(key, getattr(self, key)) for key in keys]
        info.append("-" * 20)
        info += ["{}: {}".format(key, getattr(self, key))
                 for _, _, key, _ in PAYLOAD[self.type]
                 if key != "sample_data"]
        return "{} Packet\n\t".format(self.type) + "\n\t".join(info)

    @staticmethod
    def from_string(string):
        """
        """
        if len(string) != 1024:
            msg = "Ignoring incomplete packet."
            warnings.warn(msg)
            return None
        bcd = codecs.encode(string[:16], "hex_codec").decode("ASCII")
        packet_type = string[0:2].decode("ASCII")
        experiment_number = bcd[4:6]
        year = int(bcd[6:8])
        unit_id = bcd[8:12].upper()
        time = bcd[12:24]
        byte_count = int(bcd[24:28])
        packet_sequence = int(bcd[28:32])
        payload = string[16:]
        return Packet(packet_type, experiment_number, year, unit_id, time,
                      byte_count, packet_sequence, payload)

    def _parse_payload(self, data):
        """
        """
        if self.type not in PAYLOAD:
            msg = ("Not parsing payload of packet type '{}'").format(self.type)
            warnings.warn(msg)
            self._payload = data
            return
        for offset, length, key, converter in PAYLOAD[self.type]:
            value = data[offset:offset+length]
            if converter is not None:
                value = converter(value)
            setattr(self, key, value)

    def _to_dict(self):
        """
        Convert to dictionary structure.
        """
        if self.type not in PAYLOAD:
            raise NotImplementedError()
        keys = [key for _, _, key, _ in PAYLOAD[self.type]]
        if self.type == "DT":
            keys.remove("sample_data")
        return {key: getattr(self, key) for key in keys}


def _parse_next_packet(fh):
    """
    :type fh: file like object
    """
    data = fh.read(1024)
    if not data:
        return None
    if len(data) < 1024:
        msg = "Dropping incomplete packet."
        warnings.warn(msg)
        return None
    try:
        return Packet.from_string(data)
    except:
        msg = "Caught exception parsing packet:\n{}".format(
            traceback.format_exc())
        warnings.warn(msg)
        return None


def _read_into_packetlist(filename):
    """
    """
    with open(filename, "rb") as fh:
        packets = []
        packet = _parse_next_packet(fh)
        while packet:
            if not packet:
                break
            packets.append(packet)
            packet = _parse_next_packet(fh)
    return packets


if __name__ == '__main__':
    import doctest
    doctest.testmod(exclude_empty=True)
