# Simulation

Run Mapper on Machine 1,2,3:
cat input.txt | python mapper.py > intermediate.json

Then transfer intermediate.json to Reducer Machines.

Run Reducer on Machine 4,5:
cat intermediate.json | python reducer.py > final_output.json

## Requirements
Vagrant: It is used for automates the process of setting up and configuring VMs.
