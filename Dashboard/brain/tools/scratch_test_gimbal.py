from rover_pi import PiRoverController

r = PiRoverController(host="192.168.2.3")
print(r.execute_step({"id":"t0","action":"observe","target":None}))
print(r.execute_step({"id":"t1","action":"observe","target":None}))
print(r.execute_step({"id":"t0","action":"observe","target":None}))
r.close()