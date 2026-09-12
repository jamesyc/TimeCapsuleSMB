#include "telemetry.h"
#include <assert.h>
int main(void) {
    struct telemetry_response r;
    const char *valid[] = {"", "  ", "{}", "{\"ok\":true}", "{\"DEBUG\":false}",
        "{\"nested\":{\"DEBUG\":true},\"DEBUG\":false}", "{\"x\":[null,3.5e-2,\"\\ud83d\\ude00\"]}"};
    const char *invalid[] = {"{\"DEBUG\":\"true\"}", "{\"DEBUG\":1}", "{\"DEBUG\":null}",
        "{\"DEBUG\":true,\"DEBUG\":false}", "{\"DEBUG\":true}oops", "{\"DEBUG\":true,}",
        "{\"x\":01}", "{\"x\":1e}", "{\"x\":\"\\ud800\"}", "{\"DEBUG\":true", "[]",
        "{\"DEBUG_SIGNATURE\":\"x\",\"DEBUG_SIGNATURE\":\"y\"}"};
    size_t i;
    for (i = 0; i < sizeof(valid)/sizeof(valid[0]); i++) {
        assert(!telemetry_response_parse(valid[i], strlen(valid[i]), &r)); assert(!r.debug);
    }
    for (i = 0; i < sizeof(invalid)/sizeof(invalid[0]); i++) {
        assert(telemetry_response_parse(invalid[i], strlen(invalid[i]), &r)); assert(!r.debug);
    }
    {
        const char *yes = "{\"DEBUG\":true,\"DEBUG_SIGNATURE\":\"abc\"}";
        assert(!telemetry_response_parse(yes, strlen(yes), &r));
        assert(r.debug && !strcmp(r.signature, "abc"));
    }
    return 0;
}
