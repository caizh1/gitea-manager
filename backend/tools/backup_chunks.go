// 为源站停写后保留的归档生成可复用的内容分块。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"strconv"
)

func chunkGear() [256]uint64 {
	var gear [256]uint64
	for index := range gear {
		x := uint64(index) + 0x9e3779b97f4a7c15
		x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9
		x = (x ^ (x >> 27)) * 0x94d049bb133111eb
		gear[index] = x ^ (x >> 31)
	}
	return gear
}

func contentChunks(file string, output io.Writer) error {
	source, err := os.Open(file)
	if err != nil {
		return err
	}
	defer source.Close()
	const minimum, target, maximum = 2 << 20, 8 << 20, 32 << 20
	buffer := make([]byte, 1024*1024)
	gear := chunkGear()
	digest := sha256.New()
	encoder := json.NewEncoder(output)
	var rolling uint64
	var offset int64
	var size int
	emit := func() error {
		if err := encoder.Encode(map[string]any{"offset": offset, "size": size, "sha256": hex.EncodeToString(digest.Sum(nil))}); err != nil {
			return err
		}
		offset += int64(size)
		size, rolling = 0, 0
		digest.Reset()
		return nil
	}
	for {
		n, err := source.Read(buffer)
		start := 0
		for position, item := range buffer[:n] {
			rolling = (rolling << 1) + gear[item]
			size++
			if size >= minimum && (rolling&(target-1) == 0 || size == maximum) {
				digest.Write(buffer[start : position+1])
				if err := emit(); err != nil {
					return err
				}
				start = position + 1
			}
		}
		digest.Write(buffer[start:n])
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
	}
	if size != 0 {
		if err := emit(); err != nil {
			return err
		}
	}
	return encoder.Encode(map[string]int64{"complete": offset})
}

func streamRange(file, first, count string) error {
	start, err := strconv.ParseInt(first, 10, 64)
	if err != nil || start < 0 {
		return os.ErrInvalid
	}
	length, err := strconv.ParseInt(count, 10, 64)
	if err != nil || length <= 0 || length > 32<<20 {
		return os.ErrInvalid
	}
	source, err := os.Open(file)
	if err != nil {
		return err
	}
	defer source.Close()
	if _, err := source.Seek(start, io.SeekStart); err != nil {
		return err
	}
	_, err = io.CopyN(os.Stdout, source, length)
	return err
}
