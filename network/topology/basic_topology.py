from mininet.topo import Topo
from mininet.link import TCLink


class AdaptiveQoSTopo(Topo):

    def build(self):

        # Hosts
        h1 = self.addHost("h1")
        h2 = self.addHost("h2")
        h3 = self.addHost("h3")
        h4 = self.addHost("h4")
        server = self.addHost("server")

        # Switches
        s1 = self.addSwitch("s1")
        s2 = self.addSwitch("s2")

        # Client links
        self.addLink(h1, s1)
        self.addLink(h2, s1)
        self.addLink(h3, s1)
        self.addLink(h4, s1)

        # 10 Mbps bottleneck
        self.addLink(
            s1,
            s2,
            cls=TCLink,
            bw=10,
            delay="10ms"
        )

        # Server link
        self.addLink(s2, server)


topos = {
    "adaptive": lambda: AdaptiveQoSTopo()
}