#include "../../../build/native/storage/mast.h"

int main(int argc, char **argv) {
    FILE *file;
    char *text;
    long size;
    struct tc_inventory inventory;
    size_t i;
    if (argc != 2 || (file = fopen(argv[1], "rb")) == NULL) return 2;
    if (fseek(file, 0, SEEK_END) != 0 || (size = ftell(file)) < 0 || fseek(file, 0, SEEK_SET) != 0) return 2;
    text = malloc((size_t)size + 1);
    if (text == NULL || fread(text, 1, (size_t)size, file) != (size_t)size) return 2;
    text[size] = '\0'; fclose(file);
    if (tc_mast_parse(&inventory, text) != 0) { free(text); return 3; }
    printf("valid=%d empty=%d count=%zu\n", inventory.valid, inventory.empty, inventory.count);
    for (i = 0; i < inventory.count; i++) {
        struct tc_volume *volume = &inventory.volumes[i];
        printf("%s\t%s\t%s\t%s\t%s\t%d\n", volume->disk, volume->device,
               volume->root, volume->name, volume->uuid, volume->builtin);
    }
    free(text); return 0;
}
