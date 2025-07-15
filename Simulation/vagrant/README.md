# Vagrant

Use for seting up and configuration:
``` vagrant up ```

the map.py and reduce.py are added in seting up. but if you forget or do any change in it. Use these for transferfiles manually to VMs:
run (This is an example for mapper1) :
``` vagrant ssh-config mapper1 ```

You'll see something like this:
``` Host mapper1 <br>  HostName 192.168.56.11  <br> User vagrant  <br>   Port 22  <br>  IdentityFile /path/to/.vagrant/machines/mapper1/virtualbox/private_key ```

Use that IdentityFile in your below command:
``` vscp -i /path/to/.vagrant/machines/mapper1/virtualbox/private_key map.py vagrant@192.168.56.11:/home/vagrant/ ```
