#ifndef TC_EXIT_CODES_H
#define TC_EXIT_CODES_H
/* Process exit codes shared by the service roles. The manager, deploy
 * verification and tests read them, so each value is stable. */
#define EXIT_OK 0
#define EXIT_USAGE 3
#define EXIT_INVALID_ADISK_DISK 8
#define EXIT_PLAN_FAILED 13
#define EXIT_DAEMON_STALLED 14  /* an IPC call to mDNSResponder did not return within the alarm */
#endif
