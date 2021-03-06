import time
import os
import logging
from eventlet import patcher
from eventlet import greenio
native_threading = patcher.original("threading")
native_queue = patcher.original("Queue")

import struct
import netaddr
from ryu.base import app_manager
from ryu.lib import hub
from ryu.controller.handler import set_ev_cls
from ryu.controller.handler import (MAIN_DISPATCHER,
                                    CONFIG_DISPATCHER)
from ryu.controller import ofp_event
from ryu import topology
from ryu.ofproto import ofproto_v1_0, nx_match
from ryu.ofproto import ether, inet
from ryu.lib.packet import (packet, ethernet, arp, icmp, icmpv6, ipv4, ipv6)
from ryu.lib import mac
import ryu.utils

from switch import Port, Switch
import util
import algorithm
import dest_event
import BGP4
import tap



FORMAT = '%(name)s[%(levelname)s]%(message)s'
logging.basicConfig(format=FORMAT)
LOG = logging.getLogger(__name__)

class Routing(app_manager.RyuApp):
    ARP_TIMEOUT = 600    # in seconds

    FLOW_IDLE_TIMEOUT = 60
    FLOW_HARD_TIMEOUT = 600

    def __init__(self, *args, **kwargs):
        super(Routing, self).__init__(*args, **kwargs)

        self.dpid_to_switch = {}    # dpid_to_switch[dpid] = Switch
                                    # maintains all the switches

        self.routing_algo = algorithm.Dijkstra(self.dpid_to_switch)

        if tap.device is None:
            tap.device = tap.TapDevice()

        self.filepath = 'routing.config'

        try:
            self.switch_cfg = util.read_cfg(self.filepath)
            LOG.info('Switch configuration file: %s', self.switch_cfg)
        except:
            LOG.error('File %s parse error', self.filepath)

        if util.bgper_config is None:
            try:
                util.bgper_config = util.read_bgp_config(util.BGPER_CONFIG_PATH)
                LOG.info('bgper_config: %s', util.bgper_config)
            except:
                LOG.error('File %s parse error', util.bgper_config)

        #hub.spawn(self._test)
        self._init_events()

    def _test(self):
        while True:
            self.__test()
            hub.sleep(3)

    def __test(self):
        LOG.debug('-------------------')
        for k, switch in self.dpid_to_switch.iteritems():
            LOG.debug('dpid %s, switch %s, swith name %s',
                      k, switch, switch.name)
            for k, port in switch.ports.iteritems():
                LOG.debug("port %s, dpid of port's peer %s",
                          port, port.peer_switch_dpid)
        LOG.debug('-------------------')

    def _init_pipe(self):
        """
        The pipe is for synchronization, the queue is used for store
        the packets
        """
        self._event_queue = native_queue.Queue()
        r_pipe, w_pipe = os.pipe()
        self._event_notify_send = greenio.GreenPipe(w_pipe, 'wb', 0)
        self._event_notify_recv = greenio.GreenPipe(r_pipe, 'rb', 0)

    def _init_events(self):
        """
        Init the event subsystem. Codes learned from OpenStack
        nova/virt/libvirt/driver.py
        since we meet the same problem that an eventlet green thread
        must cooperate with a native C-based library.

        - Apache License v2.0?
        - OK.
        """
        self._init_pipe()
        LOG.info('Starting native event thread')
        event_thread = native_threading.Thread(target = self.read_from_tap)
        event_thread.setDaemon(True)
        event_thread.start()
        LOG.info('Starting green dispatch thread')
        dispatch_thread = hub.spawn(self.dispatch_thread)

    def find_switch_and_port_for_dispatch(self, data):
        """
        Based on dest addr of packets in dispatch queue, find the switch obj
        and out-port number, return None if find nothing
        """
        pkt = packet.Packet(data)
        dst_ip = None
        for p in pkt.protocols:
            if isinstance(p, arp.arp):
                dst_ip = p.dst_ip
                break
            elif isinstance(p, ipv4.ipv4):
                dst_ip = p.dst
                break
            elif isinstance(p, ipv6.ipv6):
                dst_ip = p.dst
                break
            else:
                LOG.warning('Some unhandled packets sent from dispatch queue')
                pass
        if dst_ip is None:
            return None, None
        dst_ip = netaddr.IPAddress(dst_ip)
        LOG.debug("Destination IP for dispatch: %s", dst_ip)
        dst_switch = None
        dst_port = None
        for neighbor in util.bgper_config['neighbor']:
            if netaddr.IPAddress(neighbor['neighbor_ipv4']) == dst_ip or \
               netaddr.IPAddress(neighbor['neighbor_ipv6']) == dst_ip or \
               netaddr.IPAddress(neighbor['neighbor_ipv6_sma']) == dst_ip:
                dst_switch = neighbor['border_switch']
                dst_port = int(neighbor['outport_no'])
                break
        dst_switch = self.name_to_switch(dst_switch)
        return dst_switch, dst_port

    def dispatch_thread(self):

        out_switch = None
        out_port_no = None

        while True:
            # Wait to be notified that there're some events pending in queue
            try:
                _c = self._event_notify_recv.read(1)
                assert _c
            except ValueError:
                return  # will be raised when pipe is closed

            # try processing as many events as possible
            while not self._event_queue.empty():
                try:
                    data = self._event_queue.get(block = False)
                    LOG.debug('New packet in event_queue: %s',
                              ryu.utils.hex_array(data))
                    if out_switch is None or out_port_no is None:
                        out_switch, out_port_no = \
                            self.find_switch_and_port_for_dispatch(data)
                        LOG.debug('out_switch %s, out_port %s for tunneled msg',
                                  out_switch, out_port_no)
                    if out_switch and out_port_no:
                        actions = []
                        actions.append(
                                out_switch.dp.ofproto_parser.OFPActionOutput(
                                                                out_port_no))
                        out = out_switch.dp.ofproto_parser.OFPPacketOut(
                                datapath = out_switch.dp,
                                buffer_id = 0xffffffff,  # -1 in 32bit
                                in_port = ofproto_v1_0.OFPP_NONE,
                                actions = actions, data = data)
                        out_switch.dp.send_msg(out)

                except native_queue.Empty:
                    pass

    def read_from_tap(self):
        """
        Note that this method runs in a native thread
        """
        while True:
            data = tap.device.read()
            self._event_queue.put(data)
            # notify the green dispatch_thread
            c = ' '.encode()
            self._event_notify_send.write(c)
            self._event_notify_send.flush()

    def _pre_install_flow_entry(self, switch):
        # 'switch' is a Switch object
    
        # add flow entry for BGP, both IPv4 and IPv6
        rule4 = nx_match.ClsRule()
        rule4.set_dl_type(ether.ETH_TYPE_IP)
        rule4.set_nw_proto(inet.IPPROTO_TCP)
        rule4.set_tp_dst(BGP4.BGP_TCP_PORT)

        rule6 = nx_match.ClsRule()
        rule6.set_dl_type(ether.ETH_TYPE_IPV6)
        rule6.set_nw_proto(inet.IPPROTO_TCP)
        rule6.set_tp_dst(BGP4.BGP_TCP_PORT)
        
        actions = []
        actions.append(switch.dp.ofproto_parser.OFPActionOutput(
                            port = ofproto_v1_0.OFPP_CONTROLLER,
                            # 0 means "omit all the packet content", and
                            # 65535(0xffff) is the max len could be assigned
                            max_len = 65535))

        msg4 = switch.dp.ofproto_parser.NXTFlowMod(
                datapath = switch.dp, cookie = 0,
                command = switch.dp.ofproto.OFPFC_MODIFY,
                # 0 timeout means no timeout
                idle_timeout = 0, hard_timeout = 0,
                out_port = ofproto_v1_0.OFPP_CONTROLLER,
                rule = rule4, actions = actions)

        msg6 = switch.dp.ofproto_parser.NXTFlowMod(
                datapath = switch.dp, cookie = 0,
                command = switch.dp.ofproto.OFPFC_MODIFY,
                idle_timeout = 0, hard_timeout = 0,
                out_port = ofproto_v1_0.OFPP_CONTROLLER,
                rule = rule6, actions = actions)

        switch.dp.send_msg(msg4)
        switch.dp.send_msg(msg6)

        LOG.debug('Pre-installed flow entry')

    @set_ev_cls(topology.event.EventSwitchEnter)
    def switch_enter_handler(self, event):
        # very strangely, EventSwitchEnter happens after 
        # EventOFPSwitchFeatures sometimes
        dpid = event.switch.dp.id
        try:
            s = self.dpid_to_switch[dpid]
        except KeyError:
            s = Switch(event.switch.dp)
            self.dpid_to_switch[dpid] = s
            self.routing_algo.topology_last_update = time.time()

        self._pre_install_flow_entry(s)

    @set_ev_cls(topology.event.EventSwitchLeave)
    def switch_leave_handler(self, event):
        try:
            del self.dpid_to_switch[event.switch.dp.id]
            self.routing_algo.topology_last_update = time.time()
        except KeyError:
            pass

    def _update_port_link(self, dpid, port):
        switch = self.dpid_to_switch[dpid]
        p = switch.ports.get(port.port_no, None)
        if p:
            p.peer_switch_dpid = port.peer_switch_dpid
            p.peer_port_no = port.peer_port_no
        else:
            switch.ports[port.port_no] = port

        peer_switch = self.dpid_to_switch[port.peer_switch_dpid]
        switch.peer_to_local_port[peer_switch] = port.port_no


    @set_ev_cls(topology.event.EventLinkAdd)
    def link_add_handler(self, event):
        src_port = Port(port = event.link.src, peer = event.link.dst)
        dst_port = Port(port = event.link.dst, peer = event.link.src)
        self._update_port_link(src_port.dpid, src_port)
        self._update_port_link(dst_port.dpid, dst_port)
        self.routing_algo.topology_last_update = time.time()

    def _delete_link(self, port):
        try:
            switch = self.dpid_to_switch[port.dpid]
            p = switch.ports[port.port_no]
        except KeyError:
            return

        p.peer_switch_dpid = None
        p.peer_port_no = None

    @set_ev_cls(topology.event.EventLinkDelete)
    def link_delete_handler(self, event):
        try:
            switch_1 = self.dpid_to_switch[event.link.src.dpid]
            switch_2 = self.dpid_to_switch[event.link.dst.dpid]
            del switch_1.peer_to_local_port[switch_2]
            del switch_2.peer_to_local_port[switch_1]
        except KeyError:
            return

        self._delete_link(event.link.src)
        self._delete_link(event.link.dst)
        self.routing_algo.topology_last_update = time.time()


    @set_ev_cls(topology.event.EventPortAdd)
    def port_add_handler(self, event):
        port = Port(event.port)
        switch = self.dpid_to_switch[port.dpid]
        switch.ports[port.port_no] = port
        switch.update_from_config(self.switch_cfg)
        self.routing_algo.topology_last_update = time.time()

    @set_ev_cls(topology.event.EventPortDelete)
    def port_delete_handler(self, event):
        port = Port(event.port)
        try:
            switch = self.dpid_to_switch[port.dpid]
            del switch.ports[port.port_no]
            self.routing_algo.topology_last_update = time.time()
        except KeyError:
            pass


    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, [MAIN_DISPATCHER,
                                                CONFIG_DISPATCHER])
    # we must handle this event because ryu's topology discovery
    # only shows ports between switches
    def switch_feature_handler(self, event):
        dpid = event.msg.datapath_id
        try:
            switch = self.dpid_to_switch[dpid]
        except KeyError:
            self.dpid_to_switch[dpid] = Switch(event.msg.datapath)
            switch = self.dpid_to_switch[dpid]

        for port_no, port in event.msg.ports.iteritems():
            if port_no not in switch.ports:
                p = Port(port = port, dp = event.msg.datapath)
                switch.ports[port_no] = p

            p = switch.ports[port_no]

            if port_no == ofproto_v1_0.OFPP_LOCAL:
                switch.name = port.name.rstrip('\x00')
            else:
                # port.curr is a number of 32 bits, only used 12 bits in ovs
                # represents current features of the port.
                # LOCAL port doesn't have a cost value
                curr = port.curr & 0x7f	 # get last 7 bits
                p.cost = 64/curr
                print 'cost:', p.cost

        switch.update_from_config(self.switch_cfg)
        self.routing_algo.topology_last_update = time.time()

    def find_packet(self, pkt, target):
        for packet in pkt.protocols:
            try:
                if packet.protocol_name == target:
                    return packet
            except AttributeError:
                pass
        #LOG.error("Can't find packet for target %s", target)
        return None

    def _handle_arp_reply(self, msg, pkt, arp_pkt):
        switch = self.dpid_to_switch[msg.datapath.id]
        in_port_no = msg.in_port
        gateway = switch.ports[in_port_no].gateway
        pop_list = []
        if gateway and gateway.gw_ip == netaddr.IPAddress(arp_pkt.dst_ip):
            self._remember_mac_addr(switch, pkt, 4)
            for i in xrange(len(switch.msg_buffer)):
                msg, pkt, outport_no, _4or6 = switch.msg_buffer[i]
                if self.last_switch_out(msg, pkt, outport_no, _4or6):
                    pop_list.append(i)

            pop_list.sort(reverse = True)  # descending order
            for i in pop_list:             # pop from tail to head
                switch.msg_buffer.pop(i)

    def _handle_arp(self, msg, pkt, arp_pkt):
        """
            1)
            handles ARP request from hosts, about their gateways;
            only works in IPv4 since IPv6 uses NDP(ICMPv6);
            e.g. when a host need to send a packet to the gateway, it will
                firstly send an ARP to get the MAC address of the gateway
            2)
            handles ARP reply from hosts, and try to send packets currently
            stored in switch.msg_buffer
            3)
            brutally forward all ARP packets to the tap port, so the system
            protocol stack could also handle those MAC addresses
        """
        LOG.debug('Handling ARP packet %s', arp_pkt)

        # forward ARP packets to the tap port
        self.write_to_tap(pkt.data)

        if arp_pkt.opcode == arp.ARP_REPLY:
            self._handle_arp_reply(msg, pkt, arp_pkt)
            return

        if arp_pkt.opcode != arp.ARP_REQUEST:
            return

        switch = self.dpid_to_switch[msg.datapath.id]
        in_port_no = msg.in_port
        req_dst_ip = arp_pkt.dst_ip
        req_src_ip = arp_pkt.src_ip

        port = switch.ports[in_port_no]
        if port.gateway and netaddr.IPAddress(req_dst_ip) != port.gateway.gw_ip:
            return

        datapath = msg.datapath
        reply_src_mac = str(port.hw_addr)
        ether_layer = self.find_packet(pkt, 'ethernet')

        e = ethernet.ethernet(dst = ether_layer.src, src = reply_src_mac,
                                ethertype = ether.ETH_TYPE_ARP)
        a = arp.arp(hwtype = arp.ARP_HW_TYPE_ETHERNET,
                    proto = ether.ETH_TYPE_IP,
                    hlen = 6, plen = 4, opcode = arp.ARP_REPLY,
                    src_mac = reply_src_mac, src_ip = req_dst_ip,
                    dst_mac = arp_pkt.src_mac, dst_ip = req_src_ip)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()

        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
                actions = [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                data = p.data)

        LOG.debug('ARP replied: %s - %s', reply_src_mac, req_dst_ip)

    def _handle_icmp(self, msg, pkt, icmp_pkt):
        """
            reply to ICMP_ECHO_REQUEST(i.e. ping);
            may handle other types of ICMP msg in the future;
            return True if send a response
        """
        LOG.debug('Handling ICMP packet %s', icmp_pkt)

        if icmp_pkt.type != icmp.ICMP_ECHO_REQUEST:
            return False

        in_port_no = msg.in_port
        switch = self.dpid_to_switch[msg.datapath.id]
        ipv4_layer = self.find_packet(pkt, 'ipv4')
        ip_src = netaddr.IPAddress(ipv4_layer.src)
        ip_dst = netaddr.IPAddress(ipv4_layer.dst)

        if ip_dst == netaddr.IPAddress(util.bgper_config['local_ipv4']):
            self.write_to_tap(pkt.data, modifyMacAddress=True)
            LOG.debug('Forward ICMP packet to tap port.')
            return True

        need_reply = False
        for _k, p in switch.ports.iteritems():
            if p.gateway and p.gateway.gw_ip == ip_dst:
                need_reply = True
                break
        if not need_reply:
            return False

        echo_id = icmp_pkt.data.id
        echo_seq = icmp_pkt.data.seq
        echo_data = bytearray(icmp_pkt.data.data)

        icmp_data = icmp.echo(id_=echo_id, seq=echo_seq, data=echo_data)

        #send a echo reply packet
        ether_layer = self.find_packet(pkt, 'ethernet')
        ether_dst = ether_layer.src
        ether_src = str(switch.ports[in_port_no].hw_addr)
        e = ethernet.ethernet(ether_dst, ether_src, ether.ETH_TYPE_IP)
        #csum calculation should be paid attention to
        i = ipv4.ipv4(version=4, header_length=5, tos=0, total_length=0,
            identification=0, flags=0x000, offset=0, ttl=64, proto=1, csum=0,
            src=str(ip_dst), dst=str(ip_src), option=None)
        ic = icmp.icmp(type_=0, code=0, csum=0, data=icmp_data)
        p = packet.Packet()
        p.add_protocol(e)
        p.add_protocol(i)
        p.add_protocol(ic)
        p.serialize()
        datapath = msg.datapath
        datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                actions=[datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                data=p.data)
        LOG.debug('Ping replied %s -> %s', ip_dst, ip_src)
        return True

    def _handle_icmpv6(self, msg, pkt, icmpv6_pkt):
        LOG.debug('Handling ICMPv6 packet %s', icmpv6_pkt)

        ipv6_pkt = self.find_packet(pkt, 'ipv6')
        dst_addr = netaddr.IPAddress(ipv6_pkt.dst)
        if dst_addr == netaddr.IPAddress(util.bgper_config['local_ipv6']):
            self.write_to_tap(pkt.data, modifyMacAddress=True)
            # DON'T return here

        switch = self.dpid_to_switch[msg.datapath.id]
        in_port_no = msg.in_port

        if icmpv6_pkt.type_ == icmpv6.ND_NEIGHBOR_ADVERT:
            gateway = switch.ports[in_port_no].gateway
            pop_list = []
            ipv6_pkt = self.find_packet(pkt, 'ipv6')
            if gateway and gateway.gw_ipv6 == netaddr.IPAddress(ipv6_pkt.dst):
                self._remember_mac_addr(switch, pkt, 6)
                for i in xrange(len(switch.msg_buffer)):
                    msg, pkt, outport_no, _4or6 = switch.msg_buffer[i]
                    if self.last_switch_out(msg, pkt, outport_no, _4or6):
                        pop_list.append(i)

                pop_list.sort(reverse = True)
                for i in pop_list:
                    switch.msg_buffer.pop(i)

                return True
            return False

        elif icmpv6_pkt.type_ == icmpv6.ND_NEIGHBOR_SOLICIT:
            port = switch.ports[in_port_no]
            LOG.debug('ND_NEIGHBOR_SOLICIT, dest %s',
                      icmpv6_pkt.data.dst)
            if port.gateway and \
               netaddr.IPAddress(icmpv6_pkt.data.dst) != port.gateway.gw_ipv6:
                return False
            #send a ND_NEIGHBOR_REPLY packet
            ether_layer = self.find_packet(pkt, 'ethernet')
            ether_dst = ether_layer.src
            ether_src = str(port.hw_addr)
            e = ethernet.ethernet(ether_dst, ether_src, ether.ETH_TYPE_IPV6)
            ic6_data_data = icmpv6.nd_option_tla(hw_src=ether_src, data=None)
            # res: R, S, O flags for Neighbor advertisement
            # R: Router flag. Set if the sender of the advertisement is a router
            # S: Solicited flag. Set if the advertisement is in response to a
            #    solicitation
            # O: Override flag. When set, the receiving node must update its cache
            # here we must set R, S; O is optional but we decide to set
            # so res = 7
            ic6_data = icmpv6.nd_neighbor(res=7, dst=icmpv6_pkt.data.dst,
                                          option=ic6_data_data)
            ic6 = icmpv6.icmpv6(type_=icmpv6.ND_NEIGHBOR_ADVERT, code=0,
                                csum=0, data=ic6_data)
            #payload_length
            ipv6_pkt = self.find_packet(pkt, 'ipv6')
            i6 = ipv6.ipv6(version= 6, traffic_class=0, flow_label=0,
                           payload_length=32, nxt=58, hop_limit=255,
                           src=icmpv6_pkt.data.dst, dst=ipv6_pkt.src)
            p = packet.Packet()
            p.add_protocol(e)
            p.add_protocol(i6)
            p.add_protocol(ic6)
            p.serialize()
            datapath = msg.datapath
            datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                    actions=
                        [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                    data=p.data)
            LOG.debug('NA packet sent %s -> %s', icmpv6_pkt.data.dst,
                      ipv6_pkt.src)
            return True
        elif icmpv6_pkt.type_ == icmpv6.ICMPV6_ECHO_REQUEST:
            ipv6_pkt = self.find_packet(pkt, 'ipv6')

            need_reply = False
            for _k, p in switch.ports.iteritems():
                if p.gateway and \
                        p.gateway.gw_ipv6 == netaddr.IPAddress(ipv6_pkt.dst):
                    need_reply = True
                    break
            if not need_reply:
                return False

            ether_layer = self.find_packet(pkt, 'ethernet')
            ether_dst = ether_layer.src
            ether_src = str(switch.ports[in_port_no].hw_addr)
            e = ethernet.ethernet(ether_dst,ether_src,ether.ETH_TYPE_IPV6)
            ic6_data = icmpv6_pkt.data
            ic6 = icmpv6.icmpv6(type_=icmpv6.ICMPV6_ECHO_REPLY, code=0,
                                csum=0, data=ic6_data)
            i6 = ipv6.ipv6(version=6, traffic_class=0, flow_label=0,
                           payload_length=64, nxt=58, hop_limit=64,
                           src=ipv6_pkt.dst, dst=ipv6_pkt.src)
            p = packet.Packet()
            p.add_protocol(e)
            p.add_protocol(i6)
            p.add_protocol(ic6)
            p.serialize()
            datapath = msg.datapath
            datapath.send_packet_out(in_port=ofproto_v1_0.OFPP_NONE,
                    actions=
                        [datapath.ofproto_parser.OFPActionOutput(in_port_no)],
                    data=p.data)
            LOG.debug('Ping6 replied %s -> %s', ipv6_pkt.dst, ipv6_pkt.src)
            return True

        return False

    def _remember_mac_addr(self, switch, packet, _4or6):
        """
            get ip <-> mac relationship from packets and
            store them in dict ip_to_mac
        """
        time_now = time.time()
        ether_layer = self.find_packet(packet, 'ethernet')
        if _4or6 == 4:
            ip_layer = self.find_packet(packet, 'ipv4')
            if ip_layer is None:
                ip_layer = self.find_packet(packet, 'arp')
                ip_layer.src = ip_layer.src_ip
                LOG.debug('Get ARP info from ARP packet')

            LOG.debug('New ARP entry: %s - %s', ether_layer.src,
                      ip_layer.src)
        else:
            ip_layer = self.find_packet(packet, 'ipv6')
        switch.ip_to_mac[netaddr.IPAddress(ip_layer.src)] = \
                            (netaddr.EUI(ether_layer.src), time_now)

    def deploy_flow_entry(self, msg, pkt, switch_list, _4or6):
        """
            deploy flow entry into switch
            e.g. if 'switch_list' is [A, B, C], then this method will
                deploy flow entries A->B, B->C
        """
        # TODO
        # this method and last_switch_out should be restructured
        dp = msg.datapath
        length = len(switch_list)
        for i in xrange(length - 1):
            this_switch = switch_list[i]
            next_switch = switch_list[i + 1]
            outport_no = this_switch.peer_to_local_port[next_switch]
            if _4or6 == 4:
                ip_layer = self.find_packet(pkt, 'ipv4')
            else:
                ip_layer = self.find_packet(pkt, 'ipv6')

            ip_dst = netaddr.IPAddress(ip_layer.dst)
            outport = this_switch.ports[outport_no]
            mac_src = outport.hw_addr
            mac_dst = next_switch.ports[outport.peer_port_no].hw_addr
            if _4or6 == 4:
                # ip src exact match
                wildcards = ofproto_v1_0.OFPFW_ALL
                wildcards &= ~ofproto_v1_0.OFPFW_DL_TYPE
                wildcards &= ~(0x3f << ofproto_v1_0.OFPFW_NW_DST_SHIFT)

                match = dp.ofproto_parser.OFPMatch(
                        # because of wildcards, parameters other than dl_type
                        # and nw_dst could be any value
                        wildcards = wildcards, in_port = 0,
                        dl_src = 0, dl_dst = 0, dl_vlan = 0, dl_vlan_pcp = 0,
                        dl_type = ether.ETH_TYPE_IP, nw_tos = 0, nw_proto = 0,
                        nw_src = 0, nw_dst = ip_dst.value, tp_src = 0,
                        tp_dst = 0)
            else:
                rule = nx_match.ClsRule()
                rule.set_dl_type(ether.ETH_TYPE_IPV6)
                rule.set_ipv6_dst(struct.unpack('!8H', ip_dst.packed))

            actions = []
            actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                           mac_src.packed))
            actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                           mac_dst.packed))
            actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))

            if _4or6 == 4:
                mod = dp.ofproto_parser.OFPFlowMod(
                    datapath = this_switch.dp, match = match,
                    cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, actions = actions)
            else:
                mod = dp.ofproto_parser.NXTFlowMod(
                        datapath = this_switch.dp, cookie = 0,
                        command = dp.ofproto.OFPFC_MODIFY,
                        idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                        hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                        out_port = outport_no, rule = rule,
                        actions = actions)

            this_switch.dp.send_msg(mod)
            LOG.info('Flow entry deployed to %s', this_switch)

        # send packet out from the first switch
        switch = switch_list[0]
        next_switch = switch_list[1]
        outport_no = switch.peer_to_local_port[next_switch]

        outport = switch.ports[outport_no]
        mac_src = outport.hw_addr
        mac_dst = next_switch.ports[outport.peer_port_no].hw_addr
        actions = []
        actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                        mac_src.packed))
        actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                        mac_dst.packed))
        actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))

        out = dp.ofproto_parser.OFPPacketOut(
            datapath = dp, buffer_id = msg.buffer_id,
            in_port = msg.in_port, actions = actions)

        switch.dp.send_msg(out)

    def _send_arp_request(self, datapath, outport_no, dst_ip):
        src_mac_addr = \
            str(self.dpid_to_switch[datapath.id].ports[outport_no].hw_addr)
        src_ip = \
            str(self.dpid_to_switch[datapath.id].ports[outport_no].gateway.gw_ip)
        dst_ip = str(dst_ip)
        p = packet.Packet()
        e = ethernet.ethernet(dst = mac.BROADCAST_STR,
            src = src_mac_addr, ethertype = ether.ETH_TYPE_ARP)
        p.add_protocol(e)
        a = arp.arp_ip(opcode = arp.ARP_REQUEST, src_mac = src_mac_addr,
                src_ip = src_ip, dst_mac = mac.DONTCARE_STR,
                dst_ip = dst_ip)
        p.add_protocol(a)
        p.serialize()

        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
            actions = [datapath.ofproto_parser.OFPActionOutput(outport_no)],
            data = p.data)

    def _generate_dst_for_NS(self, ipv6_addr):
        """
            ICMPv6 neighbor solicitation destination addresses in ethernet
            and IP layer are multicast addresses, and could be generated as:

            IPv6:
                ff02::1:ffXX:XXXX
            where XX is the last 24 bits of the target IPv6 address

            ethernet:
                33:33:XX:XX:XX:XX
            where XX is the last 32 bits of the IPv6 multicast address,
            i.e. the address generated above, so the effective ethernet
            multicast address in this scenario is:
                33:33:ff:XX:XX:XX

            Ref: RFC 2464, RFC 2373
        """
        args = struct.unpack('!8H', ipv6_addr.packed)

        arg_6 = args[6] & 0x00ff
        arg_7head = ('%04x' % args[7])[0:2]
        arg_7tail = ('%04x' % args[7])[2:]
        ethernet_str = '33:33:ff:' + str(arg_6) + ':' + arg_7head + ':' + \
                       arg_7tail
        ethernet_addr = netaddr.EUI(ethernet_str)

        args[6] |= 0xff00
        args[0:6] = [0xff02, 0, 0, 0, 0, 1]
        args = [format(x, 'x') for x in args]
        args_str = ':'.join(args)
        ip_addr = netaddr.IPAddress(args_str)
        return ethernet_addr, ip_addr

    def _send_icmp_NS(self, datapath, outport_no, dst_ip):
        src_mac_addr = \
            str(self.dpid_to_switch[datapath.id].ports[outport_no].hw_addr)
        src_ip = \
            str(self.dpid_to_switch[datapath.id].ports[outport_no].gateway.gw_ipv6)
        p = packet.Packet()
        dst_mac, dst_ip_multicast = self._generate_dst_for_NS(dst_ip)
        dst_mac = str(dst_mac)
        dst_ip_multicast = str(dst_ip_multicast)
        dst_ip = str(dst_ip)
        e = ethernet.ethernet(dst = dst_mac, src = src_mac_addr,
                ethertype = ether.ETH_TYPE_IPV6)
        ip6 = ipv6.ipv6(version = 6, traffic_class = 0, flow_label = 0,
                # 4byte ICMP header, 4byte reserved, 16byte target address,
                # 8byte "source link-layer address" option
                # next header value for ICMPv6 is 58
                payload_length = 32, nxt = 58, hop_limit = 255,
                src = src_ip, dst = dst_ip_multicast)
        # source link-layer address
        sla_addr = icmpv6.nd_option_sla(hw_src = src_mac_addr)
        # ns for neighbor solicit; res for reserved, but actually is a flag,
        # see comments on "nd_option_tla" above
        ns = icmpv6.nd_neighbor(res = 4, dst = dst_ip, data = sla_addr)
        ic6 = icmpv6.icmpv6(type_ = icmpv6.ND_NEIGHBOR_SOLICIT, code = 0,
                # checksum = 0 then ryu calculate for you
                csum = 0, data = ns)
        p.add_protocol(e)
        p.add_protocol(ip6)
        p.add_protocol(ic6)
        p.serialize()
        datapath.send_packet_out(in_port = ofproto_v1_0.OFPP_NONE,
            actions = [datapath.ofproto_parser.OFPActionOutput(outport_no)],
            data = p.data)

    def last_switch_out(self, msg, pkt, outport_no, _4or6):
        """
        The packet has already reached the last switch and needs to be forwarded.
        Does NOT support output to routers out of the AS
        """
        if _4or6 == 4:
            ip_layer = self.find_packet(pkt, 'ipv4')
        else:
            ip_layer = self.find_packet(pkt, 'ipv6')

        dp = msg.datapath
        switch = self.dpid_to_switch[dp.id]
        ipDestAddr = netaddr.IPAddress(ip_layer.dst)

        LOG.debug('last_switch_out: switch %s, port_no %s',
                  switch, outport_no)
        try:
            # TODO introduce ARP timeout
            mac_addr = switch.ip_to_mac[ipDestAddr][0]
        except KeyError:
            # don't know MAC address yet, send ARP/ICMP message
            # and temporarily store the packets
            if _4or6 == 4:
                self._send_arp_request(msg.datapath, outport_no,
                                       ipDestAddr)
            else:
                self._send_icmp_NS(msg.datapath, outport_no,
                                   ipDestAddr)
            switch.msg_buffer.append( (msg, pkt, outport_no, _4or6) )
            return False

        if _4or6 == 4:
            # ip src exact match
            wildcards = ofproto_v1_0.OFPFW_ALL
            wildcards &= ~ofproto_v1_0.OFPFW_DL_TYPE
            wildcards &= ~(0x3f << ofproto_v1_0.OFPFW_NW_DST_SHIFT)

            match = dp.ofproto_parser.OFPMatch(
                    # because of wildcards, parameters other than dl_type
                    # and nw_dst could be any value
                    wildcards = wildcards, in_port = 0,
                    dl_src = 0, dl_dst = 0, dl_vlan = 0, dl_vlan_pcp = 0,
                    dl_type = ether.ETH_TYPE_IP, nw_tos = 0, nw_proto = 0,
                    nw_src = 0, nw_dst = ipDestAddr.value, tp_src = 0,
                    tp_dst = 0)
        else:
            rule = nx_match.ClsRule()
            rule.set_dl_type(ether.ETH_TYPE_IPV6)
            rule.set_ipv6_dst(struct.unpack('!8H', ipDestAddr.packed))

        actions = []
        actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                        switch.ports[outport_no].hw_addr.packed))
        actions.append(dp.ofproto_parser.OFPActionSetDlDst(
                        mac_addr.packed))
        actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))

        if _4or6 == 4:
            mod = dp.ofproto_parser.OFPFlowMod(
                    datapath = dp, match = match, cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, actions = actions)
        else:
            mod = dp.ofproto_parser.NXTFlowMod(
                    datapath = dp, cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, rule = rule,
                    actions = actions)

        out = dp.ofproto_parser.OFPPacketOut(
            datapath = dp, buffer_id = msg.buffer_id,
            in_port = msg.in_port, actions = actions)

        dp.send_msg(mod)
        dp.send_msg(out)
        return True

    def find_switch_of_network(self, dst_addr, _4or6):
        for dpid, switch in self.dpid_to_switch.iteritems():
            for port_no, port in switch.ports.iteritems():
                if _4or6 == 4:
                    if port.gateway and dst_addr in port.gateway.gw_ip_network:
                        if dst_addr == port.gateway.gw_ip:
                            return self.dpid_to_switch[dpid], \
                                    ofproto_v1_0.OFPP_LOCAL
                        return self.dpid_to_switch[dpid], port_no
                else:
                    if port.gateway and dst_addr in port.gateway.gw_ipv6_network:
                        if dst_addr == port.gateway.gw_ipv6:
                            return self.dpid_to_switch[dpid], \
                                    ofproto_v1_0.OFPP_LOCAL
                        return self.dpid_to_switch[dpid], port_no
        return None, None

    def name_to_switch(self, switch_name):
        for dpid, s in self.dpid_to_switch.iteritems():
            if s.name == switch_name:
                return s
        return None

    def _handle_ip(self, msg, pkt, protocol_pkt):
        LOG.debug('Handling IP packet %s', protocol_pkt)

        if isinstance(protocol_pkt, ipv4.ipv4):
            _4or6 = 4
        else:
            _4or6 = 6

        src_switch = self.dpid_to_switch[msg.datapath.id]
        self._remember_mac_addr(src_switch, pkt, _4or6)

        if _4or6 == 4:
            icmp_layer = self.find_packet(pkt, 'icmp')
            if icmp_layer and self._handle_icmp(msg, pkt, icmp_layer):
                # if icmp method handles this packet successfully,
                # further processing is not needed
                return
        else:  # _4or6 == 6
            icmpv6_layer = self.find_packet(pkt, 'icmpv6')
            if icmpv6_layer and self._handle_icmpv6(msg, pkt, icmpv6_layer):
                return

        # forward BGP packets
        tcp_layer = self.find_packet(pkt, 'tcp')
        if tcp_layer and tcp_layer.dst_port == BGP4.BGP_TCP_PORT:
            self.write_to_tap(pkt.data, modifyMacAddress=True)
            LOG.debug("BGP packet has been written to tap")
        # forward packet destined to BGP server address
        dst = netaddr.IPAddress(protocol_pkt.dst)  # could be IPv4 or IPv6
        if dst == netaddr.IPAddress(util.bgper_config['local_ipv4']) or \
           dst == netaddr.IPAddress(util.bgper_config['local_ipv6']):
            self.write_to_tap(pkt.data, modifyMacAddress=True)
            LOG.debug('Forward IP packet to tap port')
            return

        dst_switch, dst_port_no = self.find_switch_of_network(
                                netaddr.IPAddress(protocol_pkt.dst), _4or6)

        LOG.debug('First try of routing for dst %s, find switch %s, port %s',
                  protocol_pkt.dst, dst_switch, dst_port_no)

        if dst_port_no == ofproto_v1_0.OFPP_LOCAL:
            # should be handled by ICMP/ARP etc.
            return

        if dst_switch is None:
            # can't find destination in this domain
            # raise an event to `module B`
            req = dest_event.EventDestinationRequest(
                    netaddr.IPAddress(protocol_pkt.dst), _4or6)
            reply = self.send_request(req)
            if reply.dpid:
                dst_switch = self.dpid_to_switch[reply.dpid]
            elif reply.switch_name:
                dst_switch = self.name_to_switch(reply.switch_name)
            else:
                LOG.debug('Packet dropped because dst_switch == None')
                self.drop_pkt(msg)
                return

            LOG.debug('dst_switch replied from B: %s', dst_switch)
            self.forward_to_switch_and_out(src_switch, dst_switch,
                                               reply, msg, pkt, _4or6)
            return
        elif src_switch == dst_switch:
            self.last_switch_out(msg, pkt, dst_port_no, _4or6)
            return

        result = self.routing_algo.find_route(src_switch, dst_switch)
        LOG.debug('Second try of routing for dst %s, find route %s',
                  protocol_pkt.dst, result)
        if result:
            self.deploy_flow_entry(msg, pkt, result, _4or6)
        else:
            LOG.debug('Packet dropped because of no route to the switch')
            self.drop_pkt(msg)

    def write_to_tap(self, data, modifyMacAddress=False):
        # if modifyMacAddress is True, change the destination MAC address
        # to the address of tap port
        if modifyMacAddress:
            data = bytearray(data)
            struct.pack_into('!6s', data, 0, tap.device.mac_addr.packed)
            data = str(data)
            LOG.debug('Destination address changed to address of tap port')
        try:
            tap.device.write(data)
            LOG.debug('Data written to tap: %s', ryu.utils.hex_array(data))
        except tap.WriteError:
            LOG.error('Tap device write error!')
            raise tap.WriteError

    def forward_to_switch_and_out(self, src_switch, dst_switch, dst_reply,
                                  msg, pkt, _4or6):
        """
        In situation when a packet goes out of the AS
        """
        if src_switch != dst_switch:
            result = self.routing_algo.find_route(src_switch, dst_switch)
            if result:
                self.deploy_flow_entry(msg, pkt, result, _4or6)
            else:
                LOG.debug('Packet dropped because of no route to the address out of AS')
                self.drop_pkt(msg)

        self.border_switch_out(msg, pkt, dst_switch, dst_reply, _4or6)

    def border_switch_out(self, msg, pkt, dst_switch, dst_reply, _4or6):
        """
        Deploy the flow table on border switch and send the packet from
        the initial switch.
        """
        if _4or6 == 4:
            ip_layer = self.find_packet(pkt, 'ipv4')
        else:
            ip_layer = self.find_packet(pkt, 'ipv6')

        initial_dp = msg.datapath
        initial_switch = self.dpid_to_switch[initial_dp.id]
        dp = dst_switch.dp
        ipDestAddr = netaddr.IPAddress(ip_layer.dst)
        macAddr = dst_switch.ip_to_mac[ipDestAddr][0]
        outport_no = dst_reply.outport_no

        if _4or6 == 4:
            # ip src exact match
            wildcards = ofproto_v1_0.OFPFW_ALL
            wildcards &= ~ofproto_v1_0.OFPFW_DL_TYPE
            wildcards &= ~(0x3f << ofproto_v1_0.OFPFW_NW_DST_SHIFT)

            match = dp.ofproto_parser.OFPMatch(
                    # because of wildcards, parameters other than dl_type
                    # and nw_dst could be any value
                    wildcards = wildcards, in_port = 0,
                    dl_src = 0, dl_dst = 0, dl_vlan = 0, dl_vlan_pcp = 0,
                    dl_type = ether.ETH_TYPE_IP, nw_tos = 0, nw_proto = 0,
                    nw_src = 0, nw_dst = ipDestAddr.value, tp_src = 0,
                    tp_dst = 0)
        else:
            rule = nx_match.ClsRule()
            rule.set_dl_type(ether.ETH_TYPE_IPV6)
            rule.set_ipv6_dst(struct.unpack('!8H', ipDestAddr.packed))

        actions = []
        actions.append(dp.ofproto_parser.OFPActionSetDlSrc(
                       dst_switch.ports[outport_no].hw_addr.packed))
        actions.append(dp.ofproto_parser.OFPActionSetDlDst(macAddr.packed))
        actions.append(dp.ofproto_parser.OFPActionOutput(outport_no))

        if _4or6 == 4:
            mod = dp.ofproto_parser.OFPFlowMod(
                    datapath = dp, match = match, cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, actions = actions)
        else:
            mod = dp.ofproto_parser.NXTFlowMod(
                    datapath = dp, cookie = 0,
                    command = dp.ofproto.OFPFC_MODIFY,
                    idle_timeout = Routing.FLOW_IDLE_TIMEOUT,
                    hard_timeout = Routing.FLOW_HARD_TIMEOUT,
                    out_port = outport_no, rule = rule,
                    actions = actions)

        out = dp.ofproto_parser.OFPPacketOut(
            datapath = dp, buffer_id = msg.buffer_id,
            in_port = msg.in_port, actions = actions)

        dp.send_msg(mod)
        initial_dp.send_msg(out)

    def drop_pkt(self, msg):
        # Note that this drop_pkt method only drops the packet,
        # does not install any flow entries
        dp = msg.datapath
        out = dp.ofproto_parser.OFPPacketOut(datapath = dp,
                buffer_id = msg.buffer_id, in_port = msg.in_port,
                actions = [])
        dp.send_msg(out)


    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, event):
        data = event.msg.data
        pkt = packet.Packet(data)
        LOG.debug("PacketIn: %s", pkt.protocols)
        # TODO
        # handle protocols in reverse order
        for p in pkt.protocols:
            if isinstance(p, arp.arp):
                self._handle_arp(event.msg, pkt, p)
            # ipv4 and ipv6 also handle their corresponding icmp packets
            elif isinstance(p, ipv4.ipv4):
                self._handle_ip(event.msg, pkt, p)
            elif isinstance(p, ipv6.ipv6):
                self._handle_ip(event.msg, pkt, p)
            else:
                # might be more classifications here, BGP/OSPF etc.
                LOG.debug("Unhandled PacketIn %s", p)
                pass
