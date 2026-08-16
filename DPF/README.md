## Implementation

This repository provides an independent implementation of the framework proposed in the paper **“A Decentralized Data Processing Framework Based on PoUW Blockchain.”** The implementation is based on the concepts and system architecture described in the original paper and was developed to reproduce and evaluate its proposed approach.

Since some implementation-level details were not explicitly specified in the paper, several design decisions and assumptions were made during the implementation. For examle:

- **Communication protocol:** JSON-RPC (JSON-RPC) was used as the communication protocol between nodes.
- **Network communication:** HTTP-based communication was adopted for exchanging tasks, results, and blockchain-related messages between nodes.
- **Node configuration:** Configuration parameters such as node addresses, ports, and network settings were defined externally to facilitate deployment and experimentation.


These implementation choices are **not claimed to be part of the original authors' implementation**; rather, they represent our own engineering decisions made to operationalize the framework described in the paper.