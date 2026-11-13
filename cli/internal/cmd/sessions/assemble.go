// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sessions

import (
	"archive/tar"
	"bytes"
	"compress/gzip"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/evoila/meho/cli/internal/api"
)

// storedPart is one upload of a session: its row from `show`, and the
// files of its archive. Files is nil when the upload was skipped.
type storedPart struct {
	Upload api.SessionUploadPart
	Files  map[string][]byte
}

// wholeFile is one file put together from the tails of every upload.
// Missing names the uploads that were skipped; the file then holds only
// the bytes before the first one.
type wholeFile struct {
	Data    []byte
	Missing []int
}

// cleanArchivePath applies the path rules of the server
// (session_store/archive.py): no leading "/", no "." or ".." part, no
// empty part, no backslash and no control character. Like the server,
// the length limit counts characters, not bytes.
func cleanArchivePath(name string) (string, error) {
	path := strings.TrimPrefix(name, "./")
	bad := path == "" || utf8.RuneCountInString(path) > 512 || strings.HasPrefix(path, "/") || strings.Contains(path, "\\")
	for _, part := range strings.Split(path, "/") {
		if part == "" || part == "." || part == ".." {
			bad = true
		}
	}
	for _, r := range path {
		if r < 32 || r == 127 {
			bad = true
		}
	}
	if bad {
		return "", fmt.Errorf("the archive has a path that is not safe: %q", name)
	}
	return path, nil
}

// readStoredArchive returns the regular files of one stored gzip tar
// archive. It refuses links, devices, unsafe paths and a path that comes
// twice, like the server does for an upload.
func readStoredArchive(data []byte) (map[string][]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("the archive is not readable: %w", err)
	}
	defer func() { _ = zr.Close() }()
	tr := tar.NewReader(zr)
	files := map[string][]byte{}
	for {
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return files, nil
		}
		if err != nil {
			return nil, fmt.Errorf("the archive is not readable: %w", err)
		}
		if hdr.Typeflag == tar.TypeDir {
			continue
		}
		path, err := cleanArchivePath(hdr.Name)
		if err != nil {
			return nil, err
		}
		if hdr.Typeflag != tar.TypeReg {
			return nil, fmt.Errorf("the archive has a member that is not a file: %q", hdr.Name)
		}
		if _, dup := files[path]; dup {
			return nil, fmt.Errorf("the archive has %q twice", path)
		}
		content, err := io.ReadAll(tr)
		if err != nil {
			return nil, fmt.Errorf("the archive is not readable: %w", err)
		}
		files[path] = content
	}
}

// checkPartFiles makes sure an archive holds exactly the files its upload row names.
func checkPartFiles(upload api.SessionUploadPart, files map[string][]byte) error {
	for path := range upload.Files {
		if _, ok := files[path]; !ok {
			return fmt.Errorf("upload #%d names %q, but its archive does not hold it", upload.Seq, path)
		}
	}
	for path := range files {
		if _, ok := upload.Files[path]; !ok {
			return fmt.Errorf("the archive of upload #%d holds %q, which its row does not name", upload.Seq, path)
		}
	}
	return nil
}

// joinParts puts each file together from its checked tails, in seq order.
//
// Each upload row says where its tail starts in the uploaded file
// (offset) and how many uploaded bytes it adds. Those numbers count the
// bytes before the secret check, so they only prove that no tail is
// missing; the checked tails are joined as they are. A tail that does
// not start where the last one ended is an error. After a skipped
// upload, a file gets no more tails: it would have a hole.
func joinParts(parts []storedPart) (map[string]*wholeFile, error) {
	sort.Slice(parts, func(i, j int) bool { return parts[i].Upload.Seq < parts[j].Upload.Seq })
	whole := map[string]*wholeFile{}
	next := map[string]int{}
	for _, part := range parts {
		paths := make([]string, 0, len(part.Upload.Files))
		for path := range part.Upload.Files {
			paths = append(paths, path)
		}
		sort.Strings(paths)
		for _, path := range paths {
			row := part.Upload.Files[path]
			file := whole[path]
			if file == nil {
				file = &wholeFile{Data: []byte{}}
				whole[path] = file
			}
			if part.Files == nil || len(file.Missing) > 0 {
				if part.Files == nil {
					file.Missing = append(file.Missing, part.Upload.Seq)
				}
				next[path] = row.Offset + row.Added
				continue
			}
			if row.Offset != next[path] {
				return nil, fmt.Errorf("upload #%d of %q starts at byte %d, but the earlier uploads end at byte %d",
					part.Upload.Seq, path, row.Offset, next[path])
			}
			file.Data = append(file.Data, part.Files[path]...)
			next[path] = row.Offset + row.Added
		}
	}
	return whole, nil
}
