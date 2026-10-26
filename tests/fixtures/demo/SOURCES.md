# Demo library sources

The demo library behind the documentation screenshots (`tests/fixtures/demo_backend.py`, run by
`npm --prefix web run screenshots`). Every text and cover is a public-domain work from
[Project Gutenberg](https://www.gutenberg.org/); the Project Gutenberg header and licence text are not
included.

| File | Work | Gutenberg ebook | Text |
|---|---|---|---|
| `pride-and-prejudice.json`, `.jpg` | *Pride and Prejudice*, Jane Austen | [1342](https://www.gutenberg.org/ebooks/1342) | Chapters I–VIII |
| `alice-in-wonderland.json`, `.jpg` | *Alice's Adventures in Wonderland*, Lewis Carroll | [11](https://www.gutenberg.org/ebooks/11) | Chapters I–VIII |
| `wizard-of-oz.json`, `.jpg` | *The Wonderful Wizard of Oz*, L. Frank Baum | [55](https://www.gutenberg.org/ebooks/55) | Chapters I–VIII |
| `moby-dick.json`, `.jpg` | *Moby-Dick; or, The Whale*, Herman Melville | [2701](https://www.gutenberg.org/ebooks/2701) | Chapters 1–8 |
| `sherlock-holmes.json`, `.jpg` | *The Adventures of Sherlock Holmes*, Arthur Conan Doyle | [1661](https://www.gutenberg.org/ebooks/1661) | Stories I–VIII |

Each `.json` file lists the title of **every** chapter of the work (so a library card shows a realistic
count) and the text of the first eight, cut after roughly the first 4,500 characters (a screenful and a half
on a phone) at a paragraph boundary. A chapter without text raises an error if it is downloaded. Italics are
kept as `<em>`. The cover is the `pg<id>.cover.medium.jpg` image Project Gutenberg serves for the ebook.

To add a work, put its plain text through the same shape (`slug`, `title`, `author`, `gutenberg_id`,
`chapters: [{title, paragraphs}]`), add the cover as `<slug>.jpg`, list it here, and add it to `NOVELS` in
`web/e2e/screenshots.ts`.
