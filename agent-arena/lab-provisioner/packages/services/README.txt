Capstone's business services (Capstone 6.0.0-rc.4), each for its own head:

  CapstoneQMS-<version>-windows-x64.zip   head 3 (192.0.2.3), port 8611: quality records
  CapstoneERP-<version>-windows-x64.zip   head 4 (192.0.2.4), port 8612: orders, customers, shipments

They are the Capstone project's packages, unchanged; config\lab-network.json (business_services) names the
approved archive of each by SHA-256. On head 3 or head 4: START-LAB-PROVISIONER > 6 Existing machine > 10.
