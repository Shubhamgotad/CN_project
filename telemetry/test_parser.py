from iperf_parser import parse_iperf_output


sample = """
[  7]   0.00-20.00 sec  9.54 MBytes  4.00 Mbits/sec  0.000 ms  0/6906 (0%) sender
[  7]   0.00-21.20 sec  5.98 MBytes  2.37 Mbits/sec  0.450 ms  2572/6905 (37%) receiver
"""


record = parse_iperf_output(
    sample,
    source="h1",
    scenario="congestion",
    traffic_type="udp",
    requested_mbps=4,
    active_flows=4,
    bandwidth_mbps=10,
)

print(record)