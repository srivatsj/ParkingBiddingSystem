import sys
import time
from pyspark import SparkContext
from pyspark.streaming import StreamingContext
from pyspark.streaming.kafka import KafkaUtils
from pyspark.sql import SQLContext, Row
from elastic_search_wrapper.es_processor import ElasticProcessor
import json, os
from kafka import KafkaProducer
from collections import defaultdict
import datetime
import redis

def raw_data_tojson(sensor_data):
  """ Parse input json stream """
  raw_sensor = sensor_data.map(lambda k: json.loads(k[1]))
  t = raw_sensor.map(lambda x: x[x.keys()[0]])
  return t
 

if __name__ == "__main__":

    #if len(sys.argv) != 3:
        #print("Usage: kafka_wordcount.py <zk> <topic>")
        #exit(-1)

    sc = SparkContext(appName="ParkingStreamingCompute")
    ssc = StreamingContext(sc, 10)  # 10-sec window 

    #zkQuorum, topic = sys.argv[1:]
    zkQuorum = 'ec2-54-68-192-60.us-west-2.compute.amazonaws.com:2181'#os.environ['ZOOKEEPER_DNS']
    topic = 'parking_stream_topic'#os.environ['KAFKA_PARKING_TOPIC']
    topic2 = 'userbid_stream_topic'#os.environ['KAFKA_BID_TOPIC']
    kafkaBrokers = {"metadata.broker.list": "ec2-54-68-192-60.us-west-2.compute.amazonaws.com:9092, ec2-52-36-116-225.us-west-2.compute.amazonaws.com:9092, ec2-52-33-125-42.us-west-2.compute.amazonaws.com:9092"}
    park_data = KafkaUtils.createDirectStream(ssc, [topic], kafkaBrokers)
    bid_data = KafkaUtils.createDirectStream(ssc, [topic2], kafkaBrokers)
    
    print "==== Start ===="

    parkRdd = raw_data_tojson(park_data)
    bidRdd = raw_data_tojson(bid_data)
	
    # flushing redis before every window	
    def flush_redis(rdd):
	redis_client = redis.StrictRedis(host='ec2-52-36-186-92.us-west-2.compute.amazonaws.com', port=6379, db=0, password='srivats')
    	redis_client.flushdb()    
    
    bidRdd.foreachRDD(lambda x: x.foreachPartition(flush_redis))
        
    # bulk updating the occ of all lots coming through the parking stream for a 30 sec window
    def process_lots(rdd):

	print "inside elastic search updates"

	try:

	   ew = ElasticProcessor()
	   doc_list = []	
           for kv in rdd:
       		doc_list.append(kv)
	   
	   if(len(doc_list) > 0):
	   	print ew.update_document_multi(doc_list)

	except Exception as e:
	   print e
	   pass
      
    # bulk msearch through elastic to find closest lots with availability > 0 and within a mile radius of the users lat and long within the 30 sec window
    def process_bids(rdd):
	
	print "inside process bids"
	results = defaultdict(list)
	
        try:

	   ew = ElasticProcessor()
           usr_list = []
	   user_id = []
           
           for kv in rdd:
		user_id.append((kv["uid"], kv["amt"]))
		usr_list.append({"lat":  kv["lat"],"lon": kv["long"]})

	   if(len(usr_list) > 0):
	        res = ew.search_document_multi(usr_list)
		i = 0
		responses = res['responses']

        	for response in responses:
            		try:
                		hits = response['hits']['hits']
				
                		if len(hits) != 0:
				    for h in hits:
					park_lot = h['_source']
					p_id = park_lot['p_id']
					occ = park_lot['occ']
					name = park_lot['name']
					lat = park_lot['location']['lat']
					lon = park_lot['location']['lon']
					results[(p_id, occ, lat, lon, name)].append(user_id[i])
				
				i += 1	

            		except KeyError:
                		print Exception(response)
	   
	   # sorting the users by bid amount for each parking lot
	   for k,v in results.items():
		v.sort(key=lambda x: -x[1])
	   	   	    
	   return results.items()
	
	except Exception as e:
	   print Exception(e)
           pass

    # assigning users to lots only if the lot has occ and redis does not have any lot assigned to it.
    def assign_lots(rdd):
	 print "inside assign lots"
	 
	 try:
	 	#redis_client = redis.StrictRedis(host=os.environ['REDIS_HOST'], port=6379, db=0, password=os.environ['REDIS_PASSWORD'])
		redis_client = redis.StrictRedis(host='ec2-52-36-186-92.us-west-2.compute.amazonaws.com', port=6379, db=0, password='srivats')
		redis_pub = redis_client.pubsub()
		ew = ElasticProcessor()
 	        doc_list = []
         
	 	# assign users with parking spots
	 	for k,v in rdd:
	     	     occ = k[1]
		     for users in v:
			
			id = users[0]
			initial_occ = occ
			if occ == 0:
			    break
			
			if redis_client.get(id) is None:
				redis_client.set(id, k[0])
				occ -= 1
				res = '{"user_id":"' + str(id) + '", "p_id":"' + str(k[4]) + '"}'
		                redis_client.publish("bid_results", res)
		     
		     if occ != initial_occ:
		     	doc_list.append({"p_id": k[0], "occ": occ})
		
		if(len(doc_list) > 0):
                	print ew.update_document_multi(doc_list)

	 except Exception as e:
	   print Exception(e)
	   pass
         
  	     
    parkRdd.foreachRDD(lambda rdd: rdd.foreachPartition(process_lots))
    lots_map = bidRdd.mapPartitions(process_bids)
    lots_map.foreachRDD(lambda rdd: rdd.foreachPartition(assign_lots))

    print "=== End ===="
    ssc.start() 
    ssc.awaitTermination()
