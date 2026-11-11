// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Graham Power
//
// Written from Microsoft's FAT32 File System Specification, version 1.03,
// whose sections are cited by title: Boot Sector and BPB, FAT Data
// Structure, FAT Directory Structure, FAT Long Directory Entries. Field names
// (BPB_BytsPerSec, DIR_Attr, LDIR_Ord...) are the specification's.

#include "fat.h"

#include <stddef.h>
#include <string.h>

#define PATH_BYTES 512u // the longest path, in UTF-8, with its terminator
#define DEPTH_MAX 8u    // directories below the root

// FAT Directory Structure: the 32-byte entry and its attributes.
#define ENTRY_SIZE 32u
#define ENTRY_FREE 0xE5u
#define ENTRY_END 0x00u
#define NAME_E5 0x05u // a first name byte of 0xE5 is stored as 0x05
#define ATTR_VOLUME_ID 0x08u
#define ATTR_DIRECTORY 0x10u
#define ATTR_LONG_NAME 0x0Fu
#define ATTR_LONG_NAME_MASK 0x3Fu

// FAT Long Directory Entries: 13 UCS-2 characters an entry, names of up to
// 255 characters, so up to 20 entries.
#define LAST_LONG_ENTRY 0x40u
#define LONG_ENTRY_CHARACTERS 13u
#define LONG_ENTRIES_MAX 20u
// Where each of an entry's 13 characters sits: LDIR_Name1 (5), LDIR_Name2
// (6), LDIR_Name3 (2).
static const uint8_t LONG_CHARACTER_OFFSETS[LONG_ENTRY_CHARACTERS] = {
    1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30,
};

struct long_name
{
    uint16_t characters[LONG_ENTRIES_MAX * LONG_ENTRY_CHARACTERS];
    uint8_t entries;  // entries in the name being collected; 0 for none
    uint8_t expected; // the ordinal of the next entry; 0 once entry 1 is in
    uint8_t checksum; // LDIR_Chksum, the same in every entry of one name
};

struct walk
{
    struct fat_volume *volume;
    fat_visit_t visit;
    void *argument;
    struct long_name name;
    char path[PATH_BYTES];
};

struct cursor
{
    uint32_t cluster; // the cluster being read; 0 in a FAT12 or FAT16 root directory
    uint32_t index;   // the next entry in that cluster, or in the root region
    uint32_t followed;
};

static uint32_t le16(const uint8_t *bytes)
{
    return (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8;
}

static uint32_t le32(const uint8_t *bytes)
{
    return le16(bytes) | le16(bytes + 2) << 16;
}

static int read_at(const struct fat_volume *volume, uint32_t offset, void *buffer,
                   uint32_t size)
{
    return volume->read(volume->context, offset, buffer, size) == 0 ? 0 : FAT_ERR_IO;
}

static int is_power_of_two(uint32_t value)
{
    return value != 0u && (value & (value - 1u)) == 0u;
}

int fat_mount(struct fat_volume *volume, fat_read_t read, void *context)
{
    uint8_t sector[512];
    memset(volume, 0, sizeof *volume);
    volume->read = read;
    volume->context = context;
    if(read_at(volume, 0u, sector, sizeof sector) != 0)
    {
        return FAT_ERR_IO;
    }
    // Boot Sector and BPB: bytes 510 and 511 of sector 0 hold 0x55 and 0xAA.
    if(sector[510] != 0x55u || sector[511] != 0xAAu)
    {
        return FAT_ERR_FORMAT;
    }
    uint32_t bytes_per_sector = le16(sector + 11); // BPB_BytsPerSec
    uint32_t sectors_per_cluster = sector[13];     // BPB_SecPerClus
    uint32_t reserved = le16(sector + 14);         // BPB_RsvdSecCnt
    uint32_t fats = sector[16];                    // BPB_NumFATs
    uint32_t root_entries = le16(sector + 17);     // BPB_RootEntCnt
    uint32_t total = le16(sector + 19);            // BPB_TotSec16
    uint32_t fat_size = le16(sector + 22);         // BPB_FATSz16
    if(total == 0u)
    {
        total = le32(sector + 32); // BPB_TotSec32
    }
    if(fat_size == 0u)
    {
        fat_size = le32(sector + 36); // BPB_FATSz32
    }
    if(bytes_per_sector < 512u || bytes_per_sector > 4096u ||
       !is_power_of_two(bytes_per_sector) || !is_power_of_two(sectors_per_cluster) ||
       reserved == 0u || fats == 0u || fat_size == 0u || fat_size >= total ||
       total > UINT32_MAX / bytes_per_sector)
    {
        return FAT_ERR_FORMAT;
    }

    // FAT Data Structure: the count of clusters alone decides the FAT type.
    uint32_t root_sectors = (root_entries * ENTRY_SIZE + bytes_per_sector - 1u) / bytes_per_sector;
    uint32_t first_data = reserved + fats * fat_size + root_sectors;
    if(first_data >= total)
    {
        return FAT_ERR_FORMAT;
    }
    uint32_t clusters = (total - first_data) / sectors_per_cluster;
    unsigned type = clusters < 4085u ? 12u : clusters < 65525u ? 16u : 32u;
    uint32_t active_fat = 0u;
    uint32_t label_at = 43u; // BS_VolLab, FAT12 and FAT16
    if(type == 32u)
    {
        if(root_entries != 0u || le16(sector + 22) != 0u)
        {
            return FAT_ERR_FORMAT;
        }
        // BPB_ExtFlags: with bit 7 set only the FAT that bits 0-3 name is
        // in use; otherwise every FAT mirrors the first.
        uint32_t flags = le16(sector + 40);
        if((flags & 0x80u) != 0u)
        {
            active_fat = flags & 0x0Fu;
            if(active_fat >= fats)
            {
                return FAT_ERR_FORMAT;
            }
        }
        volume->root_cluster = le32(sector + 44); // BPB_RootClus
        if(volume->root_cluster < 2u || volume->root_cluster > clusters + 1u)
        {
            return FAT_ERR_FORMAT;
        }
        label_at = 71u;
    }
    else if(root_entries == 0u)
    {
        return FAT_ERR_FORMAT;
    }

    volume->type = type;
    volume->bytes_per_sector = bytes_per_sector;
    volume->total_sectors = total;
    volume->bytes_per_cluster = sectors_per_cluster * bytes_per_sector;
    volume->cluster_count = clusters;
    volume->fat_offset = (reserved + active_fat * fat_size) * bytes_per_sector;
    volume->root_offset = (reserved + fats * fat_size) * bytes_per_sector;
    volume->root_entries = root_entries;
    volume->data_offset = first_data * bytes_per_sector;
    // BS_BootSig 0x29 says BS_VolID, BS_VolLab and BS_FilSysType follow.
    if(sector[label_at - 5u] == 0x29u)
    {
        uint32_t length = 11u;
        while(length > 0u && sector[label_at + length - 1u] == ' ')
        {
            length--;
        }
        memcpy(volume->label, sector + label_at, length);
        volume->label[length] = '\0';
    }
    return 0;
}

static uint32_t cluster_offset(const struct fat_volume *volume, uint32_t cluster)
{
    return volume->data_offset + (cluster - 2u) * volume->bytes_per_cluster;
}

static int valid_cluster(const struct fat_volume *volume, uint32_t cluster)
{
    return cluster >= 2u && cluster <= volume->cluster_count + 1u;
}

// FAT Data Structure: the entry of cluster N in the FAT, and what it holds.
// The cluster that follows goes to next; 0 there means N ends its chain.
static int next_cluster(const struct fat_volume *volume, uint32_t cluster, uint32_t *next)
{
    uint8_t bytes[4];
    uint32_t value;
    uint32_t end;
    if(volume->type == 12u)
    {
        // 12-bit entries, packed: entry N starts at byte N + N / 2, in the
        // high 12 bits of its 16-bit word when N is odd.
        if(read_at(volume, volume->fat_offset + cluster + cluster / 2u, bytes, 2u) != 0)
        {
            return FAT_ERR_IO;
        }
        value = le16(bytes);
        value = (cluster & 1u) != 0u ? value >> 4 : value & 0x0FFFu;
        end = 0x0FF8u;
    }
    else if(volume->type == 16u)
    {
        if(read_at(volume, volume->fat_offset + cluster * 2u, bytes, 2u) != 0)
        {
            return FAT_ERR_IO;
        }
        value = le16(bytes);
        end = 0xFFF8u;
    }
    else
    {
        if(read_at(volume, volume->fat_offset + cluster * 4u, bytes, 4u) != 0)
        {
            return FAT_ERR_IO;
        }
        value = le32(bytes) & 0x0FFFFFFFu; // the top 4 bits are reserved
        end = 0x0FFFFFF8u;
    }
    if(value >= end)
    {
        *next = 0u;
        return 0;
    }
    // Free (0), reserved (1), bad (end - 1) and clusters past the volume
    // have no place in a chain.
    if(!valid_cluster(volume, value))
    {
        return FAT_ERR_CHAIN;
    }
    *next = value;
    return 0;
}

// The next 32-byte entry of a directory: 1, 0 past its last cluster or the
// end of the root region, or an error.
static int next_entry(const struct fat_volume *volume, struct cursor *cursor,
                      uint8_t entry[ENTRY_SIZE])
{
    uint32_t offset;
    if(cursor->cluster == 0u)
    {
        if(cursor->index >= volume->root_entries)
        {
            return 0;
        }
        offset = volume->root_offset + cursor->index * ENTRY_SIZE;
    }
    else
    {
        if(cursor->index * ENTRY_SIZE >= volume->bytes_per_cluster)
        {
            uint32_t next;
            int error = next_cluster(volume, cursor->cluster, &next);
            if(error != 0)
            {
                return error;
            }
            if(next == 0u)
            {
                return 0;
            }
            if(++cursor->followed > volume->cluster_count)
            {
                return FAT_ERR_CHAIN;
            }
            cursor->cluster = next;
            cursor->index = 0u;
        }
        offset = cluster_offset(volume, cursor->cluster) + cursor->index * ENTRY_SIZE;
    }
    cursor->index++;
    return read_at(volume, offset, entry, ENTRY_SIZE) == 0 ? 1 : FAT_ERR_IO;
}

// FAT Long Directory Entries: the checksum of the 11-byte DIR_Name that every
// long entry of the name carries, a rotate right by one bit before each byte
// is added.
static uint8_t short_name_checksum(const uint8_t *name)
{
    uint8_t sum = 0u;
    for(unsigned i = 0u; i < 11u; i++)
    {
        sum = (uint8_t)(((sum & 1u) << 7) + (sum >> 1) + name[i]);
    }
    return sum;
}

// FAT Long Directory Entries: a long name is a run of long entries stored
// just before its short entry, its last part first. The first entry of the
// run has LAST_LONG_ENTRY set in LDIR_Ord and the highest ordinal; the
// ordinals then count down to 1. An entry out of that order drops the name,
// and the short name stands.
static void collect_long_entry(struct long_name *name, const uint8_t *entry)
{
    uint8_t ordinal = entry[0] & (uint8_t)~LAST_LONG_ENTRY; // LDIR_Ord
    if((entry[0] & LAST_LONG_ENTRY) != 0u)
    {
        if(ordinal == 0u || ordinal > LONG_ENTRIES_MAX)
        {
            name->entries = 0u;
            return;
        }
        name->entries = ordinal;
        name->expected = ordinal;
        name->checksum = entry[13]; // LDIR_Chksum
    }
    if(name->entries == 0u || ordinal != name->expected || entry[13] != name->checksum)
    {
        name->entries = 0u;
        return;
    }
    uint16_t *characters = name->characters + (ordinal - 1u) * LONG_ENTRY_CHARACTERS;
    for(unsigned i = 0u; i < LONG_ENTRY_CHARACTERS; i++)
    {
        characters[i] = (uint16_t)le16(entry + LONG_CHARACTER_OFFSETS[i]);
    }
    name->expected = (uint8_t)(ordinal - 1u);
}

static int append_byte(char *path, uint32_t *length, uint8_t byte)
{
    if(*length + 1u >= PATH_BYTES)
    {
        return FAT_ERR_LIMIT;
    }
    path[(*length)++] = (char)byte;
    return 0;
}

// The long name's characters up to its 0x0000 terminator (a name that fills
// its last entry has none; the rest of that entry holds 0xFFFF), as UTF-8.
// A UTF-16 surrogate, which UCS-2 does not have, prints as '?'.
static int append_long_name(char *path, uint32_t *length, const struct long_name *name)
{
    uint32_t count = (uint32_t)name->entries * LONG_ENTRY_CHARACTERS;
    int error = 0;
    for(uint32_t i = 0u; i < count && error == 0; i++)
    {
        uint32_t character = name->characters[i];
        if(character == 0u)
        {
            break;
        }
        if(character < 0x80u)
        {
            error = append_byte(path, length, (uint8_t)character);
        }
        else if(character < 0x800u)
        {
            error = append_byte(path, length, (uint8_t)(0xC0u | character >> 6));
            if(error == 0)
            {
                error = append_byte(path, length, (uint8_t)(0x80u | (character & 0x3Fu)));
            }
        }
        else if(character >= 0xD800u && character < 0xE000u)
        {
            error = append_byte(path, length, '?');
        }
        else
        {
            error = append_byte(path, length, (uint8_t)(0xE0u | character >> 12));
            if(error == 0)
            {
                error = append_byte(path, length, (uint8_t)(0x80u | ((character >> 6) & 0x3Fu)));
            }
            if(error == 0)
            {
                error = append_byte(path, length, (uint8_t)(0x80u | (character & 0x3Fu)));
            }
        }
    }
    return error;
}

// FAT Directory Structure: DIR_Name is an 8-byte name and a 3-byte extension,
// each padded with spaces, shown as NAME.EXT. Bytes from 0x80 up are in the
// volume's OEM code page and print as '?'.
static int append_short_name(char *path, uint32_t *length, const uint8_t *entry)
{
    unsigned base = 8u;
    unsigned extension = 3u;
    int error = 0;
    while(base > 0u && entry[base - 1u] == ' ')
    {
        base--;
    }
    while(extension > 0u && entry[8u + extension - 1u] == ' ')
    {
        extension--;
    }
    for(unsigned i = 0u; i < base + extension && error == 0; i++)
    {
        if(i == base)
        {
            error = append_byte(path, length, '.');
        }
        uint8_t byte = entry[i < base ? i : 8u + (i - base)];
        if(i == 0u && byte == NAME_E5)
        {
            byte = ENTRY_FREE;
        }
        if(error == 0)
        {
            error = append_byte(path, length, byte < 0x80u ? byte : '?');
        }
    }
    return error;
}

static int walk_directory(struct walk *walk, uint32_t cluster, uint32_t length, unsigned depth)
{
    struct fat_volume *volume = walk->volume;
    struct cursor cursor = {cluster, 0u, 0u};
    uint8_t entry[ENTRY_SIZE];
    int status;
    walk->name.entries = 0u;
    while((status = next_entry(volume, &cursor, entry)) > 0)
    {
        if(entry[0] == ENTRY_END)
        {
            return 0; // FAT Directory Structure: no entry after it is in use
        }
        if(entry[0] == ENTRY_FREE)
        {
            walk->name.entries = 0u;
            continue;
        }
        uint8_t attributes = entry[11]; // DIR_Attr
        if((attributes & ATTR_LONG_NAME_MASK) == ATTR_LONG_NAME)
        {
            collect_long_entry(&walk->name, entry);
            continue;
        }
        int has_long_name = walk->name.entries != 0u && walk->name.expected == 0u &&
                            short_name_checksum(entry) == walk->name.checksum;
        if((attributes & ATTR_VOLUME_ID) != 0u || entry[0] == '.')
        {
            walk->name.entries = 0u; // the volume label, or "." and ".."
            continue;
        }
        uint32_t end = length;
        status = append_byte(walk->path, &end, '/');
        if(status == 0)
        {
            status = has_long_name ? append_long_name(walk->path, &end, &walk->name)
                                   : append_short_name(walk->path, &end, entry);
        }
        walk->name.entries = 0u;
        if(status != 0)
        {
            return status;
        }
        walk->path[end] = '\0';
        struct fat_entry found;
        found.path = walk->path;
        found.directory = (attributes & ATTR_DIRECTORY) != 0u;
        found.size = found.directory ? 0u : le32(entry + 28); // DIR_FileSize
        // DIR_FstClusHI is always 0 on FAT12 and FAT16.
        found.first_cluster = le16(entry + 26);
        if(volume->type == 32u)
        {
            found.first_cluster |= le16(entry + 20) << 16;
        }
        status = walk->visit(volume, &found, walk->argument);
        if(status == 0 && found.directory)
        {
            if(depth + 1u >= DEPTH_MAX)
            {
                status = FAT_ERR_LIMIT;
            }
            else if(!valid_cluster(volume, found.first_cluster))
            {
                status = FAT_ERR_CHAIN;
            }
            else
            {
                status = walk_directory(walk, found.first_cluster, end, depth + 1u);
            }
        }
        walk->path[length] = '\0';
        if(status != 0)
        {
            return status;
        }
    }
    return status;
}

int fat_walk(struct fat_volume *volume, fat_visit_t visit, void *argument)
{
    static struct walk walk;
    walk.volume = volume;
    walk.visit = visit;
    walk.argument = argument;
    walk.path[0] = '\0';
    // FAT12 and FAT16 keep the root directory in its own region (cursor
    // cluster 0); FAT32 keeps it in clusters from BPB_RootClus.
    return walk_directory(&walk, volume->type == 32u ? volume->root_cluster : 0u, 0u, 0u);
}

int fat_read_file(struct fat_volume *volume, const struct fat_entry *entry, fat_sink_t sink,
                  void *argument)
{
    uint8_t buffer[128];
    uint32_t cluster = entry->first_cluster;
    uint32_t remaining = entry->size;
    uint32_t followed = 0u;
    while(remaining > 0u)
    {
        if(!valid_cluster(volume, cluster) || ++followed > volume->cluster_count)
        {
            return FAT_ERR_CHAIN;
        }
        uint32_t offset = cluster_offset(volume, cluster);
        uint32_t left = remaining < volume->bytes_per_cluster ? remaining
                                                              : volume->bytes_per_cluster;
        remaining -= left;
        while(left > 0u)
        {
            uint32_t count = left < sizeof buffer ? left : (uint32_t)sizeof buffer;
            if(read_at(volume, offset, buffer, count) != 0)
            {
                return FAT_ERR_IO;
            }
            sink(buffer, count, argument);
            offset += count;
            left -= count;
        }
        if(remaining > 0u)
        {
            int error = next_cluster(volume, cluster, &cluster);
            if(error != 0)
            {
                return error;
            }
            if(cluster == 0u)
            {
                return FAT_ERR_CHAIN; // the chain ends before the file does
            }
        }
    }
    return 0;
}
