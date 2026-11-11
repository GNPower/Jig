// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// A read-only FAT12, FAT16 and FAT32 reader with long names, written from
// Microsoft's FAT32 File System Specification, version 1.03 (fat.c cites its
// sections). It reads the volume through a function, so the volume can sit
// anywhere: here, at an offset of a SPI NOR flash.

#ifndef FAT_H
#define FAT_H

#include <stdint.h>

// Reads size bytes at byte offset from the volume's first byte: 0, or a
// negative error.
typedef int (*fat_read_t)(void *context, uint32_t offset, void *buffer, uint32_t size);

struct fat_volume
{
    fat_read_t read;
    void *context;
    unsigned type; // 12, 16 or 32
    uint32_t bytes_per_sector;
    uint32_t total_sectors;
    uint32_t bytes_per_cluster;
    uint32_t cluster_count;
    uint32_t fat_offset;   // the FAT in use, in bytes from the volume's first byte
    uint32_t root_offset;  // FAT12 and FAT16: the root directory's region
    uint32_t root_entries; // FAT12 and FAT16: the region's 32-byte entries
    uint32_t root_cluster; // FAT32: the root directory's first cluster
    uint32_t data_offset;  // cluster 2
    char label[12];        // BS_VolLab without its padding; empty without one
};

struct fat_entry
{
    const char *path; // from the root, in UTF-8: "/Folder/File name.txt"
    int directory;
    uint32_t size; // bytes; 0 for a directory
    uint32_t first_cluster;
};

#define FAT_ERR_IO (-1)     // the read function failed
#define FAT_ERR_FORMAT (-2) // sector 0 holds no valid boot sector and BPB
#define FAT_ERR_CHAIN (-3)  // a cluster chain leaves the volume, loops or ends early
#define FAT_ERR_LIMIT (-4)  // a path longer, or folders deeper, than the reader holds

int fat_mount(struct fat_volume *volume, fat_read_t read, void *context);

// Calls visit for every file and directory under the root, depth first,
// each directory's entries in the order they are stored. A non-zero return
// from visit ends the walk and is returned.
typedef int (*fat_visit_t)(struct fat_volume *volume, const struct fat_entry *entry,
                           void *argument);
int fat_walk(struct fat_volume *volume, fat_visit_t visit, void *argument);

// Passes a file's contents to sink in order, a piece at a time.
typedef void (*fat_sink_t)(const uint8_t *data, uint32_t size, void *argument);
int fat_read_file(struct fat_volume *volume, const struct fat_entry *entry, fat_sink_t sink,
                  void *argument);

#endif
