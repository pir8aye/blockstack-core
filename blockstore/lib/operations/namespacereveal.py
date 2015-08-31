#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstore
    ~~~~~
    copyright: (c) 2014 by Halfmoon Labs, Inc.
    copyright: (c) 2015 by Blockstack.org
    
    This file is part of Blockstore
    
    Blockstore is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.
    
    Blockstore is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstore.  If not, see <http://www.gnu.org/licenses/>.
"""

from pybitcoin import embed_data_in_blockchain, \
    analyze_private_key, serialize_sign_and_broadcast, make_op_return_script, \
    make_pay_to_address_script, b58check_encode, b58check_decode, BlockchainInfoClient, hex_hash160

from pybitcoin.transactions.outputs import calculate_change_amount

from utilitybelt import is_hex
from binascii import hexlify, unhexlify

from ..b40 import b40_to_hex, bin_to_b40, is_b40
from ..config import *
from ..scripts import blockstore_script_to_hex, add_magic_bytes
from ..hashing import hash_name

def namespace_decay_to_float( namespace_decay_fixedpoint ):
   """
   Convert the raw namespace decay rate (a fixedpoint decimal)
   to a floating-point number.
   
   Upper 8 bits: integer 
   Lower 24 bits: decimal
   """
   
   ipart = namespace_decay_fixedpoint >> 24
   fpart = namespace_decay_fixedpoint & 0x00ffffff
   
   return ipart + (float(fpart) / (1 << 24))


def namespace_decay_to_fixpoint( namespace_decay_float ):
   """
   Convert a floating-point number to a namespace decay rate.
   Return None if invalid 
   """
   
   if namespace_decay_float < 0:
      return None 
   
   ipart = int(namespace_decay_float) 
   
   if( ipart > 255 ):
      return None 
   
   fpart = float(namespace_decay_float - ipart)
   
   fixpoint = (ipart << 24) | int(fpart * float(1 << 24))
   return fixpoint
   
   
def serialize_int( int_field, numbytes ):
   """
   Serialize an integer to a hex string that is padlen characters long.
   Raise an exception on overflow.
   """
   
   if int_field >= 2**(numbytes*8) or int_field < -(2**(numbytes*8)):
      raise Exception("Integer overflow (%s bytes)" % (numbytes) )
   
   format_str = "%%0.%sx" % (numbytes*2) 
   hex_str = format_str % int_field 
   
   if len(hex_str) % 2 != 0:
      # sometimes python cuts off the leading zero 
      hex_str = '0' + hex_str
   
   return hex_str
   
   
# name lifetime (blocks): 4 bytes (0xffffffff for infinite)
# baseline price for one-letter names (satoshis): 7 bytes
# price decay rate per letter (fixed-point decimal: 2**8 integer part, 2**24 decimal part): 4 bytes
# version: 2 bytes
# namespace ID: up to 19 bytes
def build( namespace_id, version, reveal_addr, lifetime, satoshi_cost, price_decay_rate, testset=False ):
   """
   Record to mark the beginning of a namespace import in the blockchain.
   This reveals the namespace ID, and encodes the preorder's namespace rules.
   
   Namespace ID must be base38.
   
   Format:
   
   0     2   3     7          14          18       20                    39
   |-----|---|-----|----------|-----------|--------|---------------------|
   magic op  life  cost       decay        version  ns_id
   """
   
   # sanity check 
   if not is_b40( namespace_id ) or "+" in namespace_id or namespace_id.count(".") > 0:
      raise Exception("Namespace ID '%s' has non-base-38 characters" % namespace_id)
   
   if len(namespace_id) > LENGTHS['blockchain_id_namespace_id']:
      raise Exception("Invalid namespace ID length for '%s' (expected length between 1 and %s)" % (namespace_id, LENGTHS['blockchain_id_namespace_id']))
   
   price_decay_rate_fixedpoint = namespace_decay_to_fixpoint( price_decay_rate )
   
   if price_decay_rate_fixedpoint is None:
      raise Exception("Invalid price decay rate '%s'" % price_decay_rate)
   
   if lifetime < 0 or lifetime > (2**32 - 1):
      lifetime = NAMESPACE_LIFE_INFINITE 
      
   if satoshi_cost < 0 or satoshi_cost > (2**64 - 1):
      raise Exception("Cost '%s' out of range (expected unsigned 64-bit integer)" % satoshi_cost)
   
   if price_decay_rate_fixedpoint < 0 or price_decay_rate_fixedpoint > (2**32 - 1):
      raise Exception("Decay rate '%s' out of range (expected unsigned 32-bit integer)" % price_decay_rate_fixedpoint)
   
   life_hex = serialize_int( lifetime, 4 )
   satoshi_cost_hex = serialize_int( satoshi_cost, 7 )
   price_decay_hex = serialize_int( price_decay_rate_fixedpoint, 4 )
   version_hex = serialize_int( version, 2 )
   
   readable_script = "NAMESPACE_REVEAL 0x%s 0x%s 0x%s 0x%s 0x%s" % (life_hex, satoshi_cost_hex, price_decay_hex, version_hex, hexlify(namespace_id))
   hex_script = blockstore_script_to_hex(readable_script)
   packaged_script = add_magic_bytes(hex_script, testset=testset)
   
   return packaged_script


def make_outputs( data, inputs, reveal_addr, change_addr, format='bin', op_return_amount=DEFAULT_OP_RETURN_FEE, testset=False ):
    """
    Make outputs for a register:
    [0] OP_RETURN with the name 
    [1] pay-to-address with the *reveal_addr*, not the sender's address.
    [2] change address with the NAMESPACE_PREORDER sender's address
    """
    
    total_to_send = DEFAULT_OP_RETURN_FEE * 2 + DEFAULT_DUST_FEE + len(inputs) * DEFAULT_DUST_FEE
    
    return [
        # main output
        {"script_hex": make_op_return_script(data, format=format),
         "value": DEFAULT_OP_RETURN_FEE},
    
        # register address
        {"script_hex": make_pay_to_address_script(reveal_addr),
         "value": DEFAULT_DUST_FEE},
        
        # change address
        {"script_hex": make_pay_to_address_script(change_addr),
         "value": calculate_change_amount(inputs, total_to_send, DEFAULT_OP_RETURN_FEE)},
    ]
    
    

def broadcast( namespace_id, reveal_addr, lifetime, satoshi_cost, price_decay_rate, private_key, blockchain_client, testset=False ):
   """
   Propagate a namespace.
   
   Arguments:
   namespace_id         human-readable (i.e. base-40) name of the namespace
   reveal_addr          address to own this namespace until it is ready
   lifetime:            the number of blocks for which names will be valid (pass a negative value for "infinite")
   satoshi_cost:        the base cost (i.e. cost of a 1-character name), in satoshis 
   price_decay_rate     a positive float representing the rate at which names get cheaper.  The formula is satoshi_cost / (price_decay_rate)^(name_length - 1).
   """
   
   nulldata = build( namespace_id, BLOCKSTORE_VERSION, reveal_addr, lifetime, satoshi_cost, price_decay_rate, testset=testset )
   
   # get inputs and from address
   private_key_obj, from_address, inputs = analyze_private_key(private_key, blockchain_client)
    
   # build custom outputs here
   outputs = make_outputs(nulldata, inputs, reveal_addr, from_address, format='hex')
    
   # serialize, sign, and broadcast the tx
   response = serialize_sign_and_broadcast(inputs, outputs, private_key_obj, blockchain_client)
    
   # response = {'success': True }
   response.update({'data': nulldata})
    
   return response
   

def parse( bin_payload, sender, recipient_address ):
   """
   NOTE: the first three bytes will be missing
   """
   
   off = 0
   life = None 
   cost = None 
   decay = None 
   namespace_id_len = None 
   namespace_id = None 
   
   life = int( hexlify(bin_payload[off:off+LENGTHS['blockchain_id_namespace_life']]), 16 )
   
   off += LENGTHS['blockchain_id_namespace_life']
   
   cost = int( hexlify(bin_payload[off:off+LENGTHS['blockchain_id_namespace_cost']]), 16 )
   
   off += LENGTHS['blockchain_id_namespace_cost']
   
   decay_fixedpoint = int( hexlify(bin_payload[off:off+LENGTHS['blockchain_id_namespace_price_decay']]), 16 )
   decay = namespace_decay_to_float( decay_fixedpoint )
   
   off += LENGTHS['blockchain_id_namespace_price_decay']
   
   version = int( hexlify(bin_payload[off:off+LENGTHS['blockchain_id_namespace_version']]), 16 )
   
   off += LENGTHS['blockchain_id_namespace_version']
   
   namespace_id = bin_payload[off:]
   namespace_id_hash = hash_name( namespace_id, sender, register_addr=recipient_address )
   
   return {
      'opcode': 'NAMESPACE_REVEAL',
      'lifetime': life,
      'cost': cost,
      'price_decay': decay,
      'namespace_id': namespace_id,
      'namespace_id_hash': namespace_id_hash,
      'version': version
   }

