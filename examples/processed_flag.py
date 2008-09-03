#!/usr/bin/env python
"""
    This example shows how to add or remove the POCESSED flag to
    all messages in a mailbox.
"""
import sys

from ProcImap.ProcImap import AbstractProcImap
from ProcImap.MailboxFactory import MailboxFactory

mailboxes = MailboxFactory('/home/goerz/.procimap/mailboxes.cfg')
mailbox = mailboxes["Physik"]


processor = AbstractProcImap(mailbox)
processor.set_processed()
#processor.set_unprocessed()
sys.exit(0)
