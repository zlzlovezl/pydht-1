import os
import threading
import blist
import bencode
import zope.interface
import heapq

try:
    # python2.7 and python3k
    from weakref import WeakSet
except ImportError:
    # pip install weakrefset
    # http://pypi.python.org/pypi/weakrefset
    from weakrefset import WeakSet

str_to_int = lambda s: reduce(lambda a, b: ord(b) + (a << 8), s, 0)
def int_to_str(i):
    buf = b''
    while i > 0:
        buf += chr(i % 256)
        i >>= 8
    return b''.join(reversed(buf))

class IDHTObserver(zope.interface.Interface):
    """
    """
    def someevent(foo):
        pass

import random
class TokenManager(object):
    def __init__(self, token_len=2):
        self._token_len = token_len
        self._obtained_tokens = dict() # token -> DHTNode
        self._token_ctr = random.randint(0, 2**(8*token_len)-1)

    @classmethod
    def token_to_int(self, token):
        assert len(token) == self._token_len
        return str_to_int(token)

    @classmethod
    def token_to_str(self, token):
        return int_to_str(token)

    def obtain(self, node):
        self._token_ctr += 1
        self._obtained_tokens[self._token_ctr] = node
        return self.token_to_str(self._token_ctr)

    def check(self, token, node):
        token = self.token_to_int(token)
        return self._obtained_tokens[token] == node

    def release(self, token):
        token = self.token_to_int(token)
        del self._obtained_tokens[token]


class DHTNodeID(object):
    def __init__(self, node_id):
        self._id = node_id

    @classmethod
    def from_bytea(self, bytea):
        assert 20 == len(bytea), "must be length 20 (160 bits)"
        return DHTNodeID(str_to_int(bytea))

    def distance(self, other):
        return type(self)(self.node_id ^ other.node_id)

    def __cmp__(self, other):
        return cmp(self._id, other._id)

    def __str__(self):
        return "%040x" % self._id

    def __repr__(self):
        return "<DHTNodeID %s>" % str(self)

    def to_bin(self):
        return str(self).decode('hex')

    def compact(self):
        return "\0"*6 # XXX

class DHTNode(object):
    def __init__(self, node_id, ip, port):
        self._id = DHTNodeID(node_id)
        self._ip = ip
        self._port = port

    @property
    def node_id(self):
        return self._id

    @property
    def address(self):
        return (self._ip, self._port)

    def __repr__(self):
        return "<DHTNode %s %s:%d>" % (str(self._id), self._ip, self._port)


class DHTBucketNode(object):
    """
    Can contain items who have IDs in min <= item_id < max

    This class should be Thread-safe
    """
    # TODO: fix datastructure, this tree is degenerate
    # Within this class, an `item' is a DHTNode and a `node' is part of the tree
    MAX_ITEMS = 8

    def __init__(self, our_id, minimum_id=None, maximum_id=None):
        assert our_id is not None
        self._mut_lock = threading.Lock()
        self._our_id = our_id
        self._min = minimum_id is not None and minimum_id or 0
        self._max = maximum_id is not None and maximum_id or 2**160
        self._children = None
        self._items = blist.sortedlist(key=lambda item: item.node_id)

    def is_interior_node(self):
        assert (self._items is not None) ^ (self._children is not None)
        return self._items is None and self._children is not None

    def is_leaf_node(self):
        assert (self._items is not None) ^ (self._children is not None)
        return self._children is None and self._items is not None

    def is_full(self):
        return len(self._items) >= self.MAX_ITEMS

    def accepts_id(self, id):
        return self._min <= id < self._max

    def accepts_item(self, item):
        return self._min <= item.node_id < self._max

    def __split(self):
        left = type(self)(self._our_id, self._min, (self._min + self._max)/2)
        right = type(self)(self._our_id, (self._min + self._max)/2, self._min)
        for item in filter(left.accepts_item, self._items):
            left.add_item(item)
        for item in filter(right.accepts_item, self._items):
            right.add_item(item)
        self._items = None
        self._children = (left, right)

    def __add_item(self, item):
        if self.is_interior_node():
            for ch in self._children:
                if ch.accepts_item(item):
                    return ch.add_item(item)
            raise Exception("Malrouted Node")
        elif self.is_leaf_node():
            if self.is_full():
                # bucket is splittable of bucket contains our ID
                if self.accepts_id(self._our_id):
                    self.__split()
                    return self.__add_item(item)
                return False
            self._items.add(item)
        else:
            raise Exception("Programmer Error")

    def add_item(self, item):
        """
        Lock-protected add item
        """
        if not self.accepts_item(item):
            raise Exception("Unacceptable item")
        with self._mut_lock:
            return self.__add_item(item)

    def __find_item(self, item_id):
        if self.is_interior_node():
            item = False
            for ch in self._children:
                item = item or ch.find_item(item_id)
            return item
        elif self.is_leaf_node(self):
            matching_items = filter(lambda i: i.node_id == item_id, self._items)
            if matching_items:
                return matching_items[0]
            return False
        else:
            raise Exception("Programmer Error")

    def find_item(self, item_id):
        """
        Lock-protected find item
        """
        if not self.accepts_id(item_id):
            return False
        with self._mut_lock:
            return self.__find_item(item_id)

    def all_items(self):
        """
        In-order iterable of all items in the tree
        """
        if self.is_interior_node():
            for ch in self._children:
                for i in ch.all_items():
                    yield i
        elif self.is_leaf_node():
            with self._mut_lock:
                for i in self._items:
                    yield i

    def __repr__(self):
        if self.is_interior_node():
            return "<DHTBucketNode {0x%x <= id < 0x%x} ours=%s %s>" % (
                    self._min, self._max, self.accepts_id(self._our_id),
                    repr(self._children))
        elif self.is_leaf_node():
            return "<DHTBucketNode {0x%x <= id < 0x%x} ours=%s %s>" % (
                    self._min, self._max, self.accepts_id(self._our_id),
                    repr(self._items))
        else:
            raise Exception("Programmer Error")


class TokenGenerator(object):
    pass


class DHTRouter(object):
    def __init__(self, port):
        self._our_id = DHTNodeID.from_bytea(os.urandom(20))
        self._observers = WeakSet()
        self._buckets = DHTBucketNode(self._our_id)
        self._handlers = list()

    def add_observer(self, observer_obj):
        if not IDHTObserver.providedBy(observer_obj):
            raise TypeError("add_observer argument must implement interface IDHTObserver")
        self._observers.add(observer_obj)

    def add_handler(self, **key_req):
        def decorator(handler_func):
            self._handlers.append((key_req, handler_func))
            return handler_func
        return decorator


dht_router = DHTRouter(6881)

@dht_router.add_handler(q='ping', y='q')
def ping_handler(router, ping_message):
    assert 't' in ping_message, "Malformed ping message"
    return {'t': ping_message['t'], 'y': 'r', 'r': {'id': router.node_id.to_bin()}}

@dht_router.add_handler(q='ping', y='r')
def ping_handler(router, ping_message):
    assert 'id' in ping_message['r'], "Malformed ping message"
    router.bump_node(ping_message['r']['id'])

@dht_router.add_handler(q='find_node', y='q')
def find_node_handler(router, find_node_message):
    assert 'id' in find_node_message['a']
    assert 'target' in find_node_message['a']
    req_node_id = DHTNodeID.from_bytea(find_node_message['a']['id'])

    # We'll just scan the whole list, since it is fairly small.
    nodes = heapq.nsmallest(8, dht_router.all_items(),
            lambda node: node.node_id.distance(req_node_id))
    if nodes[0].node_id == req_node_id:
        nodes = nodes[0:1]
    return {'t': find_node_message['t'], 'y': 'r', 'r': {
                'id': router.node_id.to_bin(),
                'nodes': bencode.bencode(map(DHTNodeID.compact, nodes))
            } }

@dht_router.add_handler(q='find_node', y='r')
def find_node_handler(router, find_node_message):
    pass

@dht_router.add_handler(q='get_peers', y='q')
def get_peers_handler(router, get_peers_message):
    pass

@dht_router.add_handler(q='get_peers', y='r')
def get_peers_handler(router, get_peers_message):
    pass

@dht_router.add_handler(q='announce_peer', y='q')
def announce_peer_handler(router, announce_peer_message):
    pass

@dht_router.add_handler(q='announce_peer', y='r')
def announce_peer_handler(router, announce_peer_message):
    pass


