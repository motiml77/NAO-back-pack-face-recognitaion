#Imports modules
import socket
import time
from naoqi import ALProxy

listensocket = socket.socket() #Creates an instance of socket
Port = 8000 #Port to host server on
maxConnections = 999
IP = socket.gethostname() #IP address of local machine

listensocket.bind(('192.168.0.101',Port))#pc server ip

#Starts server
listensocket.listen(maxConnections)
print("Server started at " + IP + " on port " + str(Port))

#Accepts the incomming connection
(clientsocket, address) = listensocket.accept()
print("New connection made!")

running = True

tts = ALProxy("ALTextToSpeech", "192.168.0.100", 9559)#nao ip

message = '0'
while running:
    message = clientsocket.recv(1024).decode()  # Gets the incomming message

    if int(message) == 1:
        print('hi moti')
        tts.say("Hello Moti")
    elif int(message) == 2:
        print('hi men')
        tts.say("Hello Menashe")
    time.sleep(2.0)

