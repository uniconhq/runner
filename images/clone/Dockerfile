# The clone image, ghcr.io/uniconhq/clone: what the CI's two checkout steps
# run. It is the CI's own clone plugin and two pieces of system git
# configuration, which together are the whole of what the platform needs from
# a checkout.
#
# The first is lfs.storage = /lfs-cache, which points git-lfs's object store
# at whatever volume the step mounts there. The CI's answer mounts one per org, so a
# dataset is downloaded once per org and machine, and no org's checkout is
# served an object another org's brought. The image names no volume, and runs
# as root as plugin-git does, so a volume Docker makes on first use, which
# belongs to root, needs no preparing. The setting lives in the image and not
# on the step, because a clone step given an environment block stops counting
# as a plugin and is no longer lent the credential it clones with. Nothing
# else changes: this image is trusted by the CI and runs every checkout of
# every grading run.
#
# The second is an attributes file saying every path is a large file. Every
# file a person uploads is an object in the forge's store and the commit
# holds a pointer to it, and git-lfs resolves a pointer at checkout only
# where an attributes file says that path is one: without this the workspace
# gets the pointer's text where the file should be, and a grading of it fails
# as though the contestant had submitted that text (measured 2026-10-03,
# `findings/upload-door-test.md` section 8).
#
# It is here and not in the repositories because the forge's file API applies
# a repository's own attributes to everything written through it, so a
# pointer committed under one would be stored as a second object and the
# commit would point at the pointer. Keeping the rule in the image keeps the
# repositories free of attributes and that API usable.
#
# The pattern is every path rather than the places uploads land, so that no
# convention about where files go has to be kept in step between this image
# and the platform: a file put somewhere unexpected would otherwise check out
# as its pointer's text, commit cleanly, and fail its grading as though that
# text had been submitted, which is the worst way for this to go wrong.
#
# A file that is not a pointer passes through the filter unchanged, with a
# line in the step's log saying it was not one, and git-lfs keeps a copy of
# it in the cache the step mounts. A few hundred bytes each, in a store that
# exists to hold gigabytes, is what that costs. `-text` keeps every file
# exactly as it was committed, which is what a grading should see.
# Built from the repo root: docker build -f images/clone/Dockerfile .

# woodpeckerci/plugin-git 2.10.1, by the digest of its multi-platform index,
# looked up 2026-09-30 with `docker buildx imagetools inspect`.
FROM woodpeckerci/plugin-git:2.10.1@sha256:550075c844fcec0af41d9d28f9860aa5d8c43e1e9419be4b9266dc02c2f29804

LABEL org.opencontainers.image.title="unicon-clone" \
      org.opencontainers.image.description="The CI's clone plugin, with git-lfs objects kept in /lfs-cache and every path read as a large file." \
      org.opencontainers.image.source="https://github.com/uniconhq/runner" \
      org.opencontainers.image.licenses="MIT"

RUN git config --system lfs.storage /lfs-cache \
 && printf '* filter=lfs diff=lfs merge=lfs -text\n' > /etc/unicon-gitattributes \
 && git config --system core.attributesFile /etc/unicon-gitattributes
