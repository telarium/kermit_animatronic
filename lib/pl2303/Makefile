obj-m := pl2303.o

# Resolve the build directory dynamically so this works across kernel versions.
# On Tegra/JetPack the build symlink points into the headers package;
# fall back to the standard /lib/modules path if it exists.
KDIR := $(shell readlink -f /lib/modules/$(shell uname -r)/build)
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD)/src modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD)/src clean

.PHONY: all clean
