# Real-tool golden fixtures

These files are **small, unmodified (except path sanitisation) outputs from the
real tools** exercised by `tests/parity/`. They are intentionally synthetic
sequences, not biological data.

Generation date: 2026-10-23.

Tool versions:

| Tool | Version |
| --- | --- |
| BLAST+ | 2.17.0 |
| minimap2 | 2.31-r1302 |
| MUMmer | 4.0.1 |
| KMA | 1.6.8 |
| BWA | 0.7.18-r1243 |
| mash | 2.3 |
| sourmash | 4.9.4 |
| skani | 0.3.2 |

Synthetic input (seed `20261028`, 600 bp targets unless noted):

- `targets.fa`: `t1` = random sequence A, `t2` = random sequence B
- `query.fa`: `q1` = A[100:400], `q2` = reverse-complement(B[100:400])
- `reads.fq`: two 150 bp reads from A and B
- skani fixture: 5000 bp target and its 4800 bp substring (skani needs longer
  genomes than the 600 bp used by the other tools)

Commands:

```bash
# BLAST
makeblastdb -in targets.fa -dbtype nucl -out blastdb
blastn -query query.fa -db blastdb \
  -outfmt '6 qseqid sseqid pident length mismatch gapopen gaps qstart qend sstart send evalue bitscore qlen slen stitle' \
  -evalue 1e-5 -max_target_seqs 500 -num_threads 1 -task blastn -dust no \
  > blast/blastn_nucl.tsv

# minimap2
minimap2 -x asm5 -t 1 --secondary=no -c --cs=short targets.fa query.fa \
  > minimap2/align.paf

# MUMmer
nucmer --maxmatch -c 20 -l 20 -p mummer targets.fa query.fa
show-coords -rcl -T mummer.delta > mummer/coords.txt

# KMA
kma index -i targets.fa -o kma_db
kma -t_db kma_db -i query.fa -asm -o kma_out -t 1
cp kma_out.res kma/align.res

# BWA
bwa index -p bwa_idx targets.fa
bwa mem -t 1 bwa_idx query.fa > bwa/align.sam

# mash
mash sketch -k 21 -s 1000 -S 42 -o refs target1.fa target2.fa
mash sketch -k 21 -s 1000 -S 42 -o query query.fa
mash dist -p 1 -d 1.0 refs.msh query.msh > mash/dist.tsv
mash screen -p 1 refs.msh reads.fq > mash/screen.tsv

# sourmash
sourmash sketch dna -p k=21,scaled=10 -o refs.sig target1.fa target2.fa
sourmash sketch dna -p k=21,scaled=10 -o query.sig query.fa
sourmash search query.sig refs.sig --threshold 0.0 -o sourmash/search.csv
sourmash gather query.sig refs.sig --threshold-bp 0 -o sourmash/gather.csv

# skani (5000 bp fixture)
skani sketch -o skani_db skani_target.fa
skani search -d skani_db skani_query.fa > skani/search.tsv
```

`mummer/coords.txt` line 1 was replaced with `targets.fa query.fa`; the parser
skips it anyway. Sourmash CSVs were normalised from CRLF to LF; field contents
are unchanged.

Do not regenerate these from unit-test synthetic strings: they must stay
representative of real tool output (including header order, empty CSV fields,
tag order, and CRLF handling in the parsers).
