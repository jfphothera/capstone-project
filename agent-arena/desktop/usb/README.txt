AGENT ARENA __VERSION__ - INSTALL TOOLSET (any head or orchestrator display)
==========================================================================

This one folder is the whole toolset. Nothing else belongs at the USB root.
It works on every registered display: the event service tells each computer
which display it is from its own network address. Nothing here names a head.

Prepared on the coordinator with PREPARE-USB for event service
__SERVICE__ (__ENDPOINTS__).
One-time enrollment codes, if any, are in enroll\ (one file per display).

Drag this whole AGENT-ARENA-__VERSION__ folder to the computer (the desktop is
fine), then double-click, in order:

  0-CLEAN-SLATE            (only for a full reinstall)
        Lists every Agent Arena item for this Windows user, asks you to type
        REMOVE, deletes them and checks again. The credential goes too, so a
        new enrollment code is needed. Ollama, models, network, firewall and
        power settings are never touched.

  1-DISCOVER (read-only)
        Changes nothing. Shows which display this computer is, and whether the
        event service answers.

  2-INSTALL-AND-VERIFY
        Refuses to run if this USB mixes versions. Opens the installer: check
        the identity, click Install, wait for SUCCESS, click Launch Arena.
        The one-time code for this display is filled in for you, and deleted
        from the USB only after the enrollment is proven.
        Ends with ALL CHECKS PASSED or a list of FAILs.

  3-COLLECT-EVIDENCE   (when the coordinator says "sending activity now")
        Read-only. Captures only the Arena window, for 2 minutes.

Bring the evidence\ folder back (copy the whole folder back to the USB).
