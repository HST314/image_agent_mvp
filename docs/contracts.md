# Parent/sub-agent contract v1

`DesignTaskEnvelope v1` and `DesignDeliveryEnvelope v1` are the only frozen
boundary in this phase. They define validation and fixtures only: transport,
automatic progress notification, adapters, and automatic return are out of
scope. Delivery assets must use a project-owned `artifact://` reference; local
paths and temporary provider URLs are invalid.
