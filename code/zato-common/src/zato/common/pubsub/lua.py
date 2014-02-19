# -*- coding: utf-8 -*-

"""
Copyright (C) 2014 Dariusz Suchojad <dsuch at zato.io>

Licensed under LGPLv3, see LICENSE.txt for terms and conditions.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

lua_publish = """

   local id_key = KEYS[1]
   local msg_values = KEYS[2]
   local msg_expire_at = KEYS[3]
   local last_pub_time_key = KEYS[4]
   local last_seen_producer_key = KEYS[5]

   local score = ARGV[1]
   local msg_id = ARGV[2]
   local expire_at = ARGV[3]
   local msg_value = ARGV[4]
   local topic_name = ARGV[5]
   local utc_now = ARGV[6]
   local client_id = ARGV[7]

   redis.pcall('zadd', id_key, score, msg_id)
   redis.pcall('hset', msg_values, msg_id, msg_value)
   redis.pcall('hset', msg_expire_at, msg_id, expire_at)
   redis.pcall('hset', last_pub_time_key, topic_name, utc_now)
   redis.pcall('hset', last_seen_producer_key, client_id, utc_now)
"""

lua_move_to_target_queues = """

    -- A function to copy Redis keys we operate over to a table which skips the first one, the source queue.
    local function get_target_queues(keys)
        local target_queues = {}
        if #keys == 4 then
          target_queues = {keys[4]}
        else
            for idx = 1, #KEYS do
                -- Note - the whole point is that we're skipping the first few items which are not target queues
                target_queues[idx] = KEYS[idx+3]
            end
        end
        return target_queues
    end

    local source_queue = KEYS[1]
    local backlog_full = KEYS[2]
    local unack_counter = KEYS[3]

    local is_fifo = tonumber(ARGV[1])
    local max_depth = tonumber(ARGV[2])
    local zset_command

    if is_fifo then
        zset_command = 'zrevrange'
    else
        zset_command = 'zrange'
    end

    local target_queues = get_target_queues(KEYS)
    local ids = redis.pcall(zset_command, source_queue, 0, max_depth)

    for queue_idx, target_queue in ipairs(target_queues) do
        for id_idx, id in ipairs(ids) do
            redis.call('lpush', target_queue, id)
            redis.pcall('hincrby', unack_counter, id, 1)
        end
    end

    for id_idx, id in ipairs(ids) do
        redis.pcall('zrem', source_queue, id)
    end
    """

lua_get_from_cons_queue = """

   local cons_queue = KEYS[1]
   local cons_in_flight_ids = KEYS[2]
   local cons_in_flight_data = KEYS[3]
   local msg_key = KEYS[4]

   local max_batch_size = tonumber(ARGV[1])
   local now = ARGV[2]
    
   local ids = redis.pcall('lrange', cons_queue, 0, max_batch_size)

   -- It may well be the case that there are no messages for this client
   if #ids > 0 then
       local values = redis.pcall('hmget', msg_key, unpack(ids))

       for id_idx, id in ipairs(ids) do
           redis.pcall('sadd', cons_in_flight_ids, id)
           redis.pcall('hset', cons_in_flight_data, id, now)
           redis.pcall('lrem', cons_queue, 0, id)
       end

       return values
    else
        return {}
    end
"""

lua_reject = """

   local cons_queue = KEYS[1]
   local cons_in_flight_ids = KEYS[2]
   local cons_in_flight_data = KEYS[3]
   local ids = ARGV

   redis.pcall('hdel', cons_in_flight_data, unpack(ids))

    for id_idx, id in ipairs(ids) do
        redis.pcall('srem', cons_in_flight_ids, id)
        redis.pcall('lpush', cons_queue, id)
    end
"""

lua_ack = """

   local cons_in_flight_ids = KEYS[1]
   local cons_in_flight_data = KEYS[2]
   local unack_counter = KEYS[3]
   local msg_values = KEYS[4]
   local msg_expire_at = KEYS[5]

   local ids = ARGV
   local unack_id_count = 0

    for id_idx, id in ipairs(ids) do
        redis.pcall('srem', cons_in_flight_ids, id)
        redis.pcall('hdel', cons_in_flight_data, id)
        unack_id_count = redis.pcall('hincrby', unack_counter, id, -1)

        -- It was the last confirmation we were waiting for so let's  add it to a list of IDs whose messages
        -- can be safely deleted and delete the key from a hashmap of unack'ed IDs.

        if unack_id_count == 0 then
            redis.pcall('hdel', msg_values, id)
            redis.pcall('hdel', unack_counter, id)
            redis.pcall('hdel', msg_expire_at, id)
        end

    end
"""

lua_delete_expired = """
   local consumer_msg_ids = KEYS[1]
   local cons_in_flight_ids = KEYS[2]
   local msg_values = KEYS[3]
   local msg_expire_at = KEYS[4]
   local unack_counter = KEYS[5]

   local now_utc = ARGV[1]
   local expired = {}

   -- Grab a batch of IDs to check their expiration
   local ids = redis.pcall('lrange', consumer_msg_ids, 0, 500)

   for id_idx, id in ipairs(ids) do

       -- The message may be expired but the result other than 0 means it's still in flight - we don't do anything with these.
       -- It's possible they can block the whole consumer queue but in that case a user intervention will be needed.

       if redis.pcall('sismember', id, cons_in_flight_ids) == 0 then

           -- Ok, we know the message is not in-flight so grab the time it expires at and compare it with now.
           -- Note that we're using ISO-8601 dates in the format of 2014-02-16T02:51:24.013459 so we can always
           -- compare expiration times lexicographically.

           local expire_at = redis.pcall('hget', msg_expire_at, id)

           -- The message has indeed expired so we can safely delete every piece of information on it.
           if now_utc > expire_at then
               redis.pcall('lrem', consumer_msg_ids, 0, id)
               redis.pcall('hdel', msg_values, id)
               redis.pcall('hdel', msg_expire_at, id)
               redis.pcall('hdel', unack_counter, id)
               table.insert(expired, id)
           end
       end
   end

   return expired

"""
