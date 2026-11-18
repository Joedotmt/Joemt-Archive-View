import sys


if __name__ == "__main__":
    if sys.argv[1:2] == ["--sample-video"]:
        from jvvv.sampled_preview import main

        raise SystemExit(main(sys.argv[2:]))
    else:
        from jvvv.app import main

        raise SystemExit(main())
