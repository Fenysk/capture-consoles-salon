# Pilote noyau du GL310 (AVerMedia Live Gamer Portable Lite).
#
#   make            compile gl310.ko pour le noyau en cours
#   make test       tests du modèle (espace utilisateur, sans boîtier)
#   make tables     vérifie que gl310_tables.h correspond à la trace
#   make clean

obj-m += gl310.o
gl310-y := gl310_usb.o gl310_v4l2.o

KVER ?= $(shell uname -r)
KDIR ?= /lib/modules/$(KVER)/build

.PHONY: all modules clean test tables

all: modules

modules:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	rm -f tests/test_model

test: tests/test_model
	./tests/test_model

tests/test_model: tests/test_model.c gl310_model.h
	$(CC) -std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter -o $@ tests/test_model.c

tables:
	python3 ../../tools/gen_gl310_tables.py --check
